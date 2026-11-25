"""tts-lexicon behaviour: the matcher on its own, then the real proxy against a stub engine (real
HTTP, real streaming).

The properties under test are the ones that make a lexicon safe to put in the speech path:

  1. a term is found whatever its case, accents or hyphenation, and the longest term wins;
  2. a symbol the engine cannot speak rejects the entry, because the engine would drop it silently;
  3. speech never fails because of the lexicon: no lexicon, a broken file or a non-English voice
     all pass the text through unchanged;
  4. everything that is not the speech text is relayed untouched, audio included.
"""
from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

pytest.importorskip("aiohttp")
import aiohttp  # noqa: E402
from aiohttp import web  # noqa: E402

PROXY_PY = Path(__file__).resolve().parents[1] / "services" / "voice" / "lexicon_proxy.py"


def _load_proxy():
    spec = importlib.util.spec_from_file_location("ordo_tts_lexicon", PROXY_PY)
    module = importlib.util.module_from_spec(spec)
    sys.modules["ordo_tts_lexicon"] = module
    spec.loader.exec_module(module)
    return module


proxy = _load_proxy()
VOCAB = proxy.load_vocab()

RHONE = "kˈot dy ʁˈon"
RHONE_VILLAGES = "kˈot dy ʁˈon vilˈaʒ"
ENTRIES = [
    {"term": "Côtes du Rhône", "lang": "fr", "phonemes": RHONE},
    {"term": "Côtes du Rhône Villages", "lang": "fr", "phonemes": RHONE_VILLAGES},
    {"term": "Rioja", "lang": "es", "phonemes": "rjˈoxa"},
    {"term": "Gewürztraminer", "lang": "de", "phonemes": "ɡəvˈyɾʦtɾamˌinəɾ", "aliases": ["Gewurztraminer"]},
]


def _lexicon(entries=None):
    return proxy.build_lexicon({"wine.json": {"entries": ENTRIES if entries is None else entries}}, VOCAB)


# --- the matcher -----------------------------------------------------------------------------

def test_term_is_replaced_with_an_inline_override_keeping_the_callers_spelling():
    text, matched = proxy.apply_lexicon("A glass of Côtes du Rhône, please.", _lexicon())
    assert text == f"A glass of [Côtes du Rhône](/{RHONE}/), please."
    assert matched == ["Côtes du Rhône"]


@pytest.mark.parametrize("spelling", [
    "cotes du rhone", "CÔTES DU RHÔNE", "Côtes-du-Rhône", "Cotes  du‑Rhone", "Côtes du Rhône",
])
def test_match_ignores_case_accents_and_hyphenation(spelling):
    text, matched = proxy.apply_lexicon(f"Try the {spelling} tonight.", _lexicon())
    assert f"](/{RHONE}/)" in text
    assert len(matched) == 1


def test_longest_term_wins():
    text, matched = proxy.apply_lexicon("Côtes du Rhône Villages is a step up from Côtes du Rhône.", _lexicon())
    assert text == (f"[Côtes du Rhône Villages](/{RHONE_VILLAGES}/) is a step up from "
                    f"[Côtes du Rhône](/{RHONE}/).")
    assert matched == ["Côtes du Rhône Villages", "Côtes du Rhône"]


def test_alias_matches_like_the_term():
    text, _ = proxy.apply_lexicon("An Alsace Gewurztraminer.", _lexicon())
    assert text == "An Alsace [Gewurztraminer](/ɡəvˈyɾʦtɾamˌinəɾ/)."


def test_term_inside_a_longer_word_is_not_matched():
    text, matched = proxy.apply_lexicon("Riojana and Riojas are other words.", _lexicon())
    assert matched == []
    assert text == "Riojana and Riojas are other words."


def test_existing_override_is_left_alone():
    original = "A [Rioja](/ɹiˈOhə/) the caller already marked, and a Rioja it did not."
    text, matched = proxy.apply_lexicon(original, _lexicon())
    assert text == "A [Rioja](/ɹiˈOhə/) the caller already marked, and a [Rioja](/rjˈoxa/) it did not."
    assert matched == ["Rioja"]


def test_empty_lexicon_changes_nothing():
    assert proxy.apply_lexicon("Côtes du Rhône", proxy.Lexicon.empty()) == ("Côtes du Rhône", [])


# --- validation ------------------------------------------------------------------------------

def test_entry_with_a_symbol_outside_the_engine_vocab_is_rejected():
    lexicon = _lexicon([
        {"term": "Rüdesheim", "phonemes": "ʀˈʏdəshaɪm"},       # ʀ and ʏ are not in the vocab
        {"term": "Rioja", "phonemes": "rjˈoxa"},
    ])
    assert list(lexicon.phonemes_by_key) == ["rioja"]
    assert len(lexicon.errors) == 1
    assert "Rüdesheim" in lexicon.errors[0] and "ʀ" in lexicon.errors[0] and "ʏ" in lexicon.errors[0]


@pytest.mark.parametrize("phonemes", ["kˈot/dy", "kˈot (dy)"])
def test_phonemes_that_would_end_the_override_early_are_rejected(phonemes):
    lexicon = _lexicon([{"term": "Côtes", "phonemes": phonemes}])
    assert lexicon.phonemes_by_key == {}
    assert len(lexicon.errors) == 1


def test_conflicting_duplicate_keeps_the_first_and_reports_the_second():
    lexicon = _lexicon([
        {"term": "Rioja", "phonemes": "rjˈoxa"},
        {"term": "rioja", "phonemes": "ɹiˈOhə"},
    ])
    assert lexicon.phonemes_by_key == {"rioja": "rjˈoxa"}
    assert len(lexicon.errors) == 1 and "already defined" in lexicon.errors[0]


@pytest.mark.parametrize("entry", [
    "not an object", {"phonemes": "rjˈoxa"}, {"term": "Rioja"}, {"term": "Rioja", "phonemes": "rjˈoxa", "aliases": "x"},
])
def test_malformed_entry_is_reported_and_skipped(entry):
    lexicon = _lexicon([entry, {"term": "Barolo", "phonemes": "barˈɔlo"}])
    assert list(lexicon.phonemes_by_key) == ["barolo"]
    assert len(lexicon.errors) == 1


def test_broken_file_is_reported_and_the_other_files_still_load(tmp_path):
    (tmp_path / "broken.json").write_text("{not json", encoding="utf-8")
    (tmp_path / "wine.json").write_text(json.dumps({"entries": ENTRIES}, ensure_ascii=False), encoding="utf-8")
    lexicon = proxy.load_lexicon(tmp_path, VOCAB)
    assert "rioja" in lexicon.phonemes_by_key
    assert len(lexicon.errors) == 1 and lexicon.errors[0].startswith("broken.json")


def test_missing_directory_is_an_empty_lexicon_with_an_error(tmp_path):
    lexicon = proxy.load_lexicon(tmp_path / "absent", VOCAB)
    assert lexicon.pattern is None
    assert len(lexicon.errors) == 1


@pytest.mark.parametrize(("payload", "english"), [
    ({"voice": "af_heart"}, True),
    ({"voice": "bm_george"}, True),
    ({"voice": "af_heart+af_bella"}, True),
    ({"voice": "ff_siwis"}, False),
    ({"voice": "af_heart", "lang_code": "f"}, False),
    ({"voice": "ff_siwis", "lang_code": "a"}, True),
    ({}, True),                                      # the engine's default voice is af_heart
])
def test_only_the_english_pipeline_gets_overrides(payload, english):
    assert proxy.is_english_request(payload) is english


# --- the proxy against a stub engine ---------------------------------------------------------

class StubEngine:
    """Records what it is sent; answers speech with audio written in two chunks."""

    def __init__(self) -> None:
        self.speech_bodies: list[dict] = []
        self.phonemize_bodies: list[dict] = []

    async def speech(self, request: web.Request) -> web.StreamResponse:
        self.speech_bodies.append(await request.json())
        response = web.StreamResponse(headers={"Content-Type": "audio/pcm", "X-Engine": "stub"})
        await response.prepare(request)
        await response.write(b"first-")
        await response.write(b"second")
        await response.write_eof()
        return response

    async def voices(self, request: web.Request) -> web.Response:
        return web.json_response({"voices": ["af_heart"], "query": request.query_string})

    async def phonemize(self, request: web.Request) -> web.Response:
        body = await request.json()
        self.phonemize_bodies.append(body)
        phonemes = {"Rioja": "rjˈoxa", "Bad": "ʀʏ"}.get(body["text"], "")
        return web.json_response({"phonemes": phonemes, "tokens": []})

    def app(self) -> web.Application:
        app = web.Application()
        app.router.add_post("/v1/audio/speech", self.speech)
        app.router.add_get("/v1/audio/voices", self.voices)
        app.router.add_post("/dev/phonemize", self.phonemize)
        return app


async def _serve(app: web.Application) -> tuple[web.AppRunner, str]:
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]
    return runner, f"http://127.0.0.1:{port}"


@pytest.fixture
async def stack(tmp_path):
    """A stub engine, the real proxy in front of it and a client session."""
    (tmp_path / "wine.json").write_text(json.dumps({"entries": ENTRIES}, ensure_ascii=False), encoding="utf-8")
    engine = StubEngine()
    engine_runner, engine_url = await _serve(engine.app())
    service = proxy.LexiconService(upstream=engine_url, directory=tmp_path, vocab=VOCAB, reload_seconds=3600)
    proxy_runner, proxy_url = await _serve(proxy.make_app(service))
    async with aiohttp.ClientSession() as session:
        yield engine, service, proxy_url, session, tmp_path
    await proxy_runner.cleanup()
    await engine_runner.cleanup()


async def test_speech_text_is_rewritten_and_the_audio_is_relayed(stack):
    engine, service, proxy_url, session, _ = stack
    request = {"model": "kokoro", "voice": "af_heart", "response_format": "pcm", "input": "Open the Rioja."}
    async with session.post(f"{proxy_url}/v1/audio/speech", json=request) as response:
        assert response.status == 200
        assert response.headers["Content-Type"] == "audio/pcm"
        assert response.headers["X-Engine"] == "stub"
        assert await response.read() == b"first-second"
    assert engine.speech_bodies == [{**request, "input": "Open the [Rioja](/rjˈoxa/)."}]
    assert service.status()["rewritten_requests"] == 1
    assert service.status()["top_terms"] == [("rioja", 1)]


async def test_non_english_voice_is_passed_through_unchanged(stack):
    engine, _, proxy_url, session, _ = stack
    request = {"model": "kokoro", "voice": "ff_siwis", "input": "Un verre de Rioja."}
    async with session.post(f"{proxy_url}/v1/audio/speech", json=request) as response:
        assert response.status == 200
        await response.read()
    assert engine.speech_bodies == [request]


async def test_other_routes_are_relayed_with_their_query_string(stack):
    _, _, proxy_url, session, _ = stack
    async with session.get(f"{proxy_url}/v1/audio/voices?x=1") as response:
        assert await response.json() == {"voices": ["af_heart"], "query": "x=1"}


async def test_lexicon_is_reloaded_when_a_file_changes(stack):
    _, service, proxy_url, session, directory = stack
    assert service.reload() is False
    (directory / "more.json").write_text(
        json.dumps({"entries": [{"term": "Barolo", "phonemes": "barˈɔlo"}]}, ensure_ascii=False), encoding="utf-8")
    assert service.reload() is True
    async with session.post(f"{proxy_url}/_lexicon/preview", json={"text": "A Barolo."}) as response:
        assert await response.json() == {"text": "A [Barolo](/barˈɔlo/).", "matched": ["Barolo"]}


async def test_status_reports_terms_and_errors(stack):
    _, service, proxy_url, session, directory = stack
    (directory / "bad.json").write_text(
        json.dumps({"entries": [{"term": "Rüdesheim", "phonemes": "ʀʏ"}]}, ensure_ascii=False), encoding="utf-8")
    service.reload()
    async with session.get(f"{proxy_url}/_lexicon/status") as response:
        body = await response.json()
    assert body["terms"] == 4          # the Gewurztraminer alias folds to its term's key
    assert body["files"] == ["bad.json", "wine.json"]
    assert len(body["errors"]) == 1 and "Rüdesheim" in body["errors"][0]


async def test_draft_asks_the_engine_in_the_terms_language_and_flags_unspeakable_symbols(stack):
    engine, _, proxy_url, session, _ = stack
    terms = [{"term": "Rioja", "lang": "es"}, {"term": "Bad", "lang": "fr"}, {"term": "Spätlese", "lang": "de"}]
    async with session.post(f"{proxy_url}/_lexicon/draft", json={"terms": terms}) as response:
        entries = (await response.json())["entries"]
    assert engine.phonemize_bodies == [{"text": "Rioja", "language": "e"}, {"text": "Bad", "language": "f"}]
    assert entries[0] == {"term": "Rioja", "lang": "es", "phonemes": "rjˈoxa"}
    assert "cannot speak" in entries[1]["error"]
    assert "no front end" in entries[2]["error"]


async def test_engine_down_is_a_502_not_a_hang(tmp_path):
    service = proxy.LexiconService(upstream="http://127.0.0.1:9", directory=tmp_path, vocab=VOCAB, reload_seconds=3600)
    runner, proxy_url = await _serve(proxy.make_app(service))
    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(f"{proxy_url}/v1/audio/speech", json={"voice": "af_heart", "input": "Hi."}) as r:
                assert r.status == 502
                assert (await r.json())["error"]["type"] == "upstream_error"
    finally:
        await runner.cleanup()


async def test_engine_dying_mid_stream_drops_the_caller_instead_of_hanging_or_faking_a_full_answer(tmp_path):
    async def dies_mid_stream(request: web.Request) -> web.StreamResponse:
        response = web.StreamResponse(headers={"Content-Type": "audio/pcm", "Content-Length": "1000"})
        await response.prepare(request)
        await response.write(b"partial")
        request.transport.close()           # the engine is killed with most of the body unsent
        return response

    engine_app = web.Application()
    engine_app.router.add_post("/v1/audio/speech", dies_mid_stream)
    engine_runner, engine_url = await _serve(engine_app)
    service = proxy.LexiconService(upstream=engine_url, directory=tmp_path, vocab=VOCAB, reload_seconds=3600)
    proxy_runner, proxy_url = await _serve(proxy.make_app(service))
    try:
        timeout = aiohttp.ClientTimeout(total=10)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            with pytest.raises(aiohttp.ClientError):
                async with session.post(f"{proxy_url}/v1/audio/speech", json={"voice": "af_heart", "input": "Hi."}) as r:
                    assert r.status == 200
                    await r.read()
    finally:
        await proxy_runner.cleanup()
        await engine_runner.cleanup()
