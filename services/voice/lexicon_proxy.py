"""tts-lexicon: a pronunciation lexicon in front of the text-to-speech engine.

WHY THIS EXISTS
    Kokoro's English text front end reads every word it does not know with American English
    letter-to-sound rules. A word with an accent can never be in its dictionary (the lookup is
    ASCII-only), so "Côtes du Rhône" is spoken "cots doo rone" and "Gewürztraminer" starts with
    a "j". The acoustic model is not at fault: it never receives the right phonemes.

    The engine already accepts an inline override, `[text](/phonemes/)`, which bypasses that
    front end for the span. This service applies such overrides from a lexicon, so a caller keeps
    sending plain text and a pronunciation is fixed once, as data, for every caller.

WHAT IT DOES
    It is a pass-through HTTP proxy for the engine (same port, same API). One request is changed:
    the `input` of `POST /v1/audio/speech`, where every lexicon term becomes an inline override.
    Everything else (voices, the dev endpoints, streaming audio) is relayed untouched.

WHAT IT IS NOT
    It is not a grapheme-to-phoneme engine and it does not guess. A term that is not in the
    lexicon is spoken exactly as before.

THE LEXICON
    Every `*.json` file in `LEXICON_DIR`, each shaped
        {"entries": [{"term": "Côtes du Rhône", "phonemes": "kˈot dy ʁˈon",
                      "lang": "fr", "aliases": ["Côtes-du-Rhône"]}]}
    `term` and `aliases` are matched whole-word, ignoring case, accents and the difference between
    a hyphen and a space; the longest term wins. `phonemes` is in the ENGINE'S alphabet (the vocab
    in kokoro_vocab.json), not standard IPA. `lang` is documentation for whoever maintains the file.
    The directory is re-read when its content changes: no restart to add a term.

FAILURE BEHAVIOUR (all deliberate)
    * An entry with a symbol the engine does not know is REJECTED and listed at
      `/_lexicon/status`. The engine drops unknown symbols without an error, so a typo would
      otherwise silently remove a sound.
    * A missing, empty or unreadable lexicon never fails speech: the text passes through unchanged.
    * A request for a non-English voice passes through unchanged: the override syntax exists only
      in the engine's English pipeline.
    * Engine unreachable -> 502 with a JSON error. The proxy never invents audio.
    * Engine dies mid-response -> the caller's connection is dropped, as it would be without the
      proxy. Truncated audio is never presented as a complete answer.

CONFIG (env)
    LEXICON_UPSTREAM         base URL of the engine (required)
    LEXICON_LISTEN_PORT      port this service listens on (default 8880, the engine's)
    LEXICON_DIR              directory of lexicon JSON files (default /lexicon)
    LEXICON_RELOAD_SECONDS   how often the directory is checked for changes (default 5)
"""
from __future__ import annotations

import asyncio
import contextlib
import dataclasses
import hashlib
import json
import logging
import os
import re
import sys
import unicodedata
from collections import Counter
from pathlib import Path
from typing import Any

import aiohttp
from aiohttp import web

LOG = logging.getLogger("tts-lexicon")

# Reserved prefix for this service's own endpoints, so it cannot collide with an engine route.
CONTROL_PREFIX = "/_lexicon"
SPEECH_PATH = "/v1/audio/speech"
PHONEMIZE_PATH = "/dev/phonemize"

# Hop-by-hop headers that must not be forwarded across a proxy hop (RFC 9110 §7.6.1).
HOP_BY_HOP = frozenset({
    "connection", "keep-alive", "proxy-authenticate", "proxy-authorization",
    "te", "trailers", "transfer-encoding", "upgrade",
})

VOCAB_FILE = Path(__file__).with_name("kokoro_vocab.json")

# The engine's override syntax is `[text](/phonemes/)`. Its parser ends the phoneme string at the
# first of these, so a phoneme string may not contain them even though two are in the vocab.
OVERRIDE_FORBIDDEN = frozenset("/()")
# Matches an override already present in the text, so a caller's own override is never rewritten.
EXISTING_OVERRIDE = re.compile(r"\[[^\]\[]+\]\(/[^/()]+/\)")

# The engine's language codes for the languages a lexicon entry can be drafted in. German is absent
# because the engine has no German front end: those entries are written by hand.
DRAFT_LANGUAGES = {"en": "a", "fr": "f", "it": "i", "es": "e", "pt": "p"}
# The engine's English pipelines. Overrides are parsed only there.
ENGLISH_LANG_CODES = frozenset({"a", "b"})

APOSTROPHES = "’‘ʼ`"
HYPHENS = "‐‑‒–"


def load_vocab(path: Path = VOCAB_FILE) -> frozenset[str]:
    """The engine's phoneme symbols (the keys of the model's `vocab`), as one-character strings."""
    symbols = json.loads(path.read_text(encoding="utf-8"))["symbols"]
    return frozenset(symbols)


def fold_char(ch: str) -> str:
    """One character -> the one character it is matched as: lower case, no accent, plain hyphen
    and apostrophe. Always returns exactly one character, so a folded string keeps the positions
    of the text it came from."""
    if ch in APOSTROPHES:
        return "'"
    if ch in HYPHENS:
        return "-"
    base = "".join(c for c in unicodedata.normalize("NFKD", ch) if not unicodedata.combining(c))
    lowered = base.lower()
    if len(lowered) == 1:
        return lowered
    lowered = ch.lower()
    return lowered if len(lowered) == 1 else ch


def fold(text: str) -> str:
    """Fold NFC text for matching. `len(fold(t)) == len(t)` for NFC input."""
    return "".join(fold_char(ch) for ch in text)


def term_key(term: str) -> str:
    """A term's identity: folded, with every run of spaces and hyphens as one space."""
    folded = fold(unicodedata.normalize("NFC", term)).strip()
    return re.sub(r"[\s\-]+", " ", folded)


def invalid_symbols(phonemes: str, vocab: frozenset[str]) -> list[str]:
    """The symbols in `phonemes` the engine would drop or that would end the override early."""
    bad = [ch for ch in phonemes if ch not in vocab or ch in OVERRIDE_FORBIDDEN]
    return sorted(set(bad))


@dataclasses.dataclass(frozen=True)
class Lexicon:
    """A loaded lexicon: term key -> phonemes, the pattern that finds the terms, and what was
    wrong with the files it came from."""
    phonemes_by_key: dict[str, str]
    pattern: re.Pattern[str] | None
    errors: tuple[str, ...]
    files: tuple[str, ...]
    digest: str

    @classmethod
    def empty(cls) -> Lexicon:
        return cls(phonemes_by_key={}, pattern=None, errors=(), files=(), digest="")


def compile_pattern(keys: list[str]) -> re.Pattern[str] | None:
    """One alternation over every term key, longest first so "côtes du rhône villages" beats
    "côtes du rhône". A space in a key matches any run of spaces and hyphens in the text."""
    if not keys:
        return None
    alternatives = []
    for key in sorted(keys, key=len, reverse=True):
        words = [re.escape(word) for word in key.split(" ")]
        alternatives.append(r"[\s\-]+".join(words))
    return re.compile(r"(?<!\w)(?:" + "|".join(alternatives) + r")(?!\w)")


def build_lexicon(documents: dict[str, Any], vocab: frozenset[str], digest: str = "") -> Lexicon:
    """Validate parsed lexicon files (`{file name: parsed JSON}`) into a Lexicon. A bad entry is
    reported and skipped; the rest of the file still loads."""
    phonemes_by_key: dict[str, str] = {}
    owner_by_key: dict[str, str] = {}
    errors: list[str] = []
    for file_name in sorted(documents):
        document = documents[file_name]
        entries = document.get("entries") if isinstance(document, dict) else None
        if not isinstance(entries, list):
            errors.append(f'{file_name}: expected an object with an "entries" list')
            continue
        for index, entry in enumerate(entries):
            where = f"{file_name}: entry {index}"
            if not isinstance(entry, dict):
                errors.append(f"{where}: expected an object")
                continue
            term = entry.get("term")
            phonemes = entry.get("phonemes")
            aliases = entry.get("aliases", [])
            if not isinstance(term, str) or not term.strip():
                errors.append(f'{where}: "term" must be a non-empty string')
                continue
            where = f"{file_name}: {term!r}"
            if not isinstance(phonemes, str) or not phonemes.strip():
                errors.append(f'{where}: "phonemes" must be a non-empty string')
                continue
            if not isinstance(aliases, list) or not all(isinstance(a, str) for a in aliases):
                errors.append(f'{where}: "aliases" must be a list of strings')
                continue
            bad = invalid_symbols(phonemes, vocab)
            if bad:
                errors.append(f"{where}: phonemes contain symbols the engine cannot speak: {' '.join(bad)}")
                continue
            for spelling in [term, *aliases]:
                key = term_key(spelling)
                if not key:
                    errors.append(f"{where}: alias {spelling!r} is empty after normalizing")
                    continue
                if key in phonemes_by_key and phonemes_by_key[key] != phonemes:
                    errors.append(f"{where}: {spelling!r} is already defined by {owner_by_key[key]}; keeping that one")
                    continue
                phonemes_by_key[key] = phonemes.strip()
                owner_by_key[key] = where
    return Lexicon(
        phonemes_by_key=phonemes_by_key,
        pattern=compile_pattern(list(phonemes_by_key)),
        errors=tuple(errors),
        files=tuple(sorted(documents)),
        digest=digest,
    )


def read_lexicon_dir(directory: Path) -> tuple[dict[str, Any], list[str], str]:
    """Read every `*.json` under `directory`. Returns the parsed documents, the read errors and a
    digest of the raw bytes (the reload check compares digests, never timestamps: a bind mount's
    mtime is not trustworthy on every host)."""
    documents: dict[str, Any] = {}
    errors: list[str] = []
    hasher = hashlib.sha256()
    if not directory.is_dir():
        return documents, [f"{directory}: lexicon directory does not exist"], "missing"
    for path in sorted(directory.glob("*.json")):
        try:
            raw = path.read_bytes()
        except OSError as exc:
            errors.append(f"{path.name}: unreadable ({exc})")
            continue
        hasher.update(path.name.encode("utf-8"))
        hasher.update(raw)
        try:
            documents[path.name] = json.loads(raw.decode("utf-8-sig"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            errors.append(f"{path.name}: not valid JSON ({exc})")
    return documents, errors, hasher.hexdigest()


def load_lexicon(directory: Path, vocab: frozenset[str]) -> Lexicon:
    documents, read_errors, digest = read_lexicon_dir(directory)
    lexicon = build_lexicon(documents, vocab, digest)
    return dataclasses.replace(lexicon, errors=tuple(read_errors) + lexicon.errors)


def _rewrite_segment(segment: str, lexicon: Lexicon, matched: list[str]) -> str:
    """Rewrite one stretch of text that holds no existing override."""
    assert lexicon.pattern is not None
    folded = fold(segment)
    pieces: list[str] = []
    cursor = 0
    for match in lexicon.pattern.finditer(folded):
        phonemes = lexicon.phonemes_by_key[term_key(match.group(0))]
        original = segment[match.start():match.end()]
        pieces.append(segment[cursor:match.start()])
        pieces.append(f"[{original}](/{phonemes}/)")
        matched.append(original)
        cursor = match.end()
    pieces.append(segment[cursor:])
    return "".join(pieces)


def apply_lexicon(text: str, lexicon: Lexicon) -> tuple[str, list[str]]:
    """Replace every lexicon term in `text` with an inline override. Returns the new text and the
    spellings that were matched, in order. Overrides already in the text are left alone."""
    if lexicon.pattern is None or not text:
        return text, []
    text = unicodedata.normalize("NFC", text)
    matched: list[str] = []
    pieces: list[str] = []
    cursor = 0
    for existing in EXISTING_OVERRIDE.finditer(text):
        pieces.append(_rewrite_segment(text[cursor:existing.start()], lexicon, matched))
        pieces.append(existing.group(0))
        cursor = existing.end()
    pieces.append(_rewrite_segment(text[cursor:], lexicon, matched))
    return "".join(pieces), matched


def is_english_request(payload: dict[str, Any]) -> bool:
    """True when the engine will run its English pipeline for this speech request: an explicit
    English `lang_code`, or (without one) a voice whose name starts with the English prefixes
    (`af_`, `am_`, `bf_`, `bm_`). A request that names neither is English too: the engine's
    default voice is `af_heart`. An operator who sets the engine's default voice to a non-English
    one must have callers name their voice."""
    lang_code = str(payload.get("lang_code") or "").strip().lower()
    if lang_code:
        return lang_code[:1] in ENGLISH_LANG_CODES
    voice = str(payload.get("voice") or "").strip().lower()
    if not voice:
        return True
    return voice[:1] in ENGLISH_LANG_CODES


class LexiconService:
    """The proxy's state: the engine it fronts, the lexicon it applies and its counters."""

    def __init__(self, upstream: str, directory: Path, vocab: frozenset[str], reload_seconds: float) -> None:
        self.upstream = upstream.rstrip("/")
        self.directory = directory
        self.vocab = vocab
        self.reload_seconds = reload_seconds
        self.lexicon = Lexicon.empty()
        self.term_hits: Counter[str] = Counter()
        self.speech_requests = 0
        self.rewritten_requests = 0
        self.session: aiohttp.ClientSession | None = None

    def reload(self) -> bool:
        """Re-read the lexicon directory when its content changed. Returns True on a change."""
        _documents, _errors, digest = read_lexicon_dir(self.directory)
        if digest == self.lexicon.digest:
            return False
        self.lexicon = load_lexicon(self.directory, self.vocab)
        LOG.info("lexicon loaded: %d terms from %d files, %d errors",
                 len(self.lexicon.phonemes_by_key), len(self.lexicon.files), len(self.lexicon.errors))
        for error in self.lexicon.errors:
            LOG.warning("lexicon: %s", error)
        return True

    async def reload_forever(self) -> None:
        while True:
            await asyncio.sleep(self.reload_seconds)
            try:
                await asyncio.to_thread(self.reload)
            except Exception:  # noqa: BLE001 - a bad reload must never stop speech
                LOG.exception("lexicon reload failed; keeping the lexicon already loaded")

    def rewrite_speech_body(self, body: bytes) -> bytes:
        """The speech request body with the lexicon applied to `input`. Any body that is not the
        expected JSON is returned untouched: the engine answers it with its own error."""
        try:
            payload = json.loads(body)
        except (UnicodeDecodeError, json.JSONDecodeError):
            return body
        if not isinstance(payload, dict) or not isinstance(payload.get("input"), str):
            return body
        self.speech_requests += 1
        if not is_english_request(payload):
            return body
        rewritten, matched = apply_lexicon(payload["input"], self.lexicon)
        if not matched:
            return body
        self.rewritten_requests += 1
        self.term_hits.update(term_key(spelling) for spelling in matched)
        LOG.info("speech: %d override(s): %s", len(matched), ", ".join(matched))
        payload["input"] = rewritten
        return json.dumps(payload, ensure_ascii=False).encode("utf-8")

    def status(self) -> dict[str, Any]:
        return {
            "upstream": self.upstream,
            "directory": str(self.directory),
            "files": list(self.lexicon.files),
            "terms": len(self.lexicon.phonemes_by_key),
            "errors": list(self.lexicon.errors),
            "speech_requests": self.speech_requests,
            "rewritten_requests": self.rewritten_requests,
            "top_terms": self.term_hits.most_common(50),
        }


SERVICE: web.AppKey[LexiconService] = web.AppKey("service", LexiconService)


def _forward_headers(headers: Any) -> dict[str, str]:
    skipped = HOP_BY_HOP | {"host", "content-length"}
    return {name: value for name, value in headers.items() if name.lower() not in skipped}


async def proxy(request: web.Request) -> web.StreamResponse:
    """Relay one request to the engine and stream its answer back as it arrives (the engine
    streams audio sentence by sentence; buffering it here would add the whole clip's latency)."""
    service = request.app[SERVICE]
    assert service.session is not None
    body = await request.read()
    if request.method == "POST" and request.path == SPEECH_PATH:
        body = service.rewrite_speech_body(body)
    url = service.upstream + request.rel_url.raw_path_qs
    response: web.StreamResponse | None = None
    try:
        async with service.session.request(
            request.method, url, headers=_forward_headers(request.headers), data=body or None,
        ) as upstream_response:
            response = web.StreamResponse(status=upstream_response.status, reason=upstream_response.reason)
            for name, value in upstream_response.headers.items():
                if name.lower() not in HOP_BY_HOP and name.lower() != "content-length":
                    response.headers[name] = value
            await response.prepare(request)
            async for chunk in upstream_response.content.iter_any():
                await response.write(chunk)
            await response.write_eof()
            return response
    except aiohttp.ClientError as exc:
        if response is not None and response.prepared:
            # The stream broke after the status line went out - either the engine died or the caller
            # aborted, and both land here. A second response cannot be sent on this connection, and
            # ending the body cleanly would tell the caller the truncated audio is complete. Drop the
            # connection, which is what the caller sees without a proxy.
            LOG.warning(
                "stream broke mid-response for %s %s (engine death or caller abort); dropping the caller: %s",
                request.method, request.path, exc,
            )
            if request.transport is not None:
                request.transport.close()
            return response
        LOG.warning("engine unreachable for %s %s: %s", request.method, request.path, exc)
        return web.json_response(
            {"error": {"message": f"text-to-speech engine unreachable: {exc}", "type": "upstream_error"}},
            status=502,
        )


async def health(request: web.Request) -> web.Response:
    return web.json_response({"status": "ok", "terms": len(request.app[SERVICE].lexicon.phonemes_by_key)})


async def status(request: web.Request) -> web.Response:
    return web.json_response(request.app[SERVICE].status())


async def preview(request: web.Request) -> web.Response:
    """What the engine would be sent for a text, without speaking it."""
    payload = await _json_object(request)
    text = payload.get("text")
    if not isinstance(text, str):
        raise web.HTTPBadRequest(text='expected {"text": "..."}')
    rewritten, matched = apply_lexicon(text, request.app[SERVICE].lexicon)
    return web.json_response({"text": rewritten, "matched": matched})


async def draft(request: web.Request) -> web.Response:
    """Draft lexicon entries: ask the engine's own front end for each term's phonemes in the
    term's SOURCE language. A draft is a starting point for a listening check, never ground
    truth; entries with symbols the engine cannot speak come back flagged."""
    service = request.app[SERVICE]
    assert service.session is not None
    payload = await _json_object(request)
    terms = payload.get("terms")
    if not isinstance(terms, list):
        raise web.HTTPBadRequest(text='expected {"terms": [{"term": "...", "lang": "fr"}]}')
    entries: list[dict[str, Any]] = []
    for item in terms:
        term = item.get("term") if isinstance(item, dict) else None
        lang = str(item.get("lang") or "") if isinstance(item, dict) else ""
        if not isinstance(term, str) or not term.strip():
            entries.append({"term": term, "lang": lang, "error": '"term" must be a non-empty string'})
            continue
        if lang not in DRAFT_LANGUAGES:
            supported = ", ".join(sorted(DRAFT_LANGUAGES))
            entries.append({"term": term, "lang": lang, "error": f"no front end for {lang!r} (supported: {supported})"})
            continue
        try:
            async with service.session.post(
                service.upstream + PHONEMIZE_PATH, json={"text": term, "language": DRAFT_LANGUAGES[lang]},
            ) as upstream_response:
                answer = await upstream_response.json(content_type=None)
        except (aiohttp.ClientError, json.JSONDecodeError) as exc:
            entries.append({"term": term, "lang": lang, "error": f"engine did not answer: {exc}"})
            continue
        phonemes = answer.get("phonemes") if isinstance(answer, dict) else None
        if not isinstance(phonemes, str) or not phonemes.strip():
            entries.append({"term": term, "lang": lang, "error": f"engine returned no phonemes: {answer!r}"})
            continue
        entry: dict[str, Any] = {"term": term, "lang": lang, "phonemes": phonemes.strip()}
        bad = invalid_symbols(entry["phonemes"], service.vocab)
        if bad:
            entry["error"] = f"symbols the engine cannot speak: {' '.join(bad)}"
        entries.append(entry)
    return web.json_response({"entries": entries})


async def _json_object(request: web.Request) -> dict[str, Any]:
    try:
        payload = await request.json()
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise web.HTTPBadRequest(text=f"body is not valid JSON: {exc}") from exc
    if not isinstance(payload, dict):
        raise web.HTTPBadRequest(text="body must be a JSON object")
    return payload


async def _lifecycle(app: web.Application):
    service = app[SERVICE]
    # No total timeout: a long clip streams for as long as the engine speaks. Connecting is bounded.
    service.session = aiohttp.ClientSession(
        timeout=aiohttp.ClientTimeout(total=None, sock_connect=10), auto_decompress=False,
    )
    await asyncio.to_thread(service.reload)
    reloader = asyncio.create_task(service.reload_forever())
    yield
    reloader.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await reloader
    await service.session.close()


def make_app(service: LexiconService) -> web.Application:
    app = web.Application(client_max_size=16 * 1024 * 1024)
    app[SERVICE] = service
    app.cleanup_ctx.append(_lifecycle)
    app.router.add_get(f"{CONTROL_PREFIX}/health", health)
    app.router.add_get(f"{CONTROL_PREFIX}/status", status)
    app.router.add_post(f"{CONTROL_PREFIX}/preview", preview)
    app.router.add_post(f"{CONTROL_PREFIX}/draft", draft)
    app.router.add_route("*", "/{tail:.*}", proxy)
    return app


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="[tts-lexicon] %(levelname)s %(message)s", stream=sys.stdout)
    upstream = os.environ.get("LEXICON_UPSTREAM", "").strip()
    if not upstream:
        LOG.error("LEXICON_UPSTREAM is required (the engine's base URL)")
        return 2
    service = LexiconService(
        upstream=upstream,
        directory=Path(os.environ.get("LEXICON_DIR", "/lexicon").strip() or "/lexicon"),
        vocab=load_vocab(),
        reload_seconds=float(os.environ.get("LEXICON_RELOAD_SECONDS", "5").strip() or "5"),
    )
    port = int(os.environ.get("LEXICON_LISTEN_PORT", "8880").strip() or "8880")
    web.run_app(make_app(service), host="0.0.0.0", port=port, print=None)
    return 0


if __name__ == "__main__":
    sys.exit(main())
