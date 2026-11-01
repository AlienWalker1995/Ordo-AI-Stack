"""Call a local OpenAI-compatible LLM to clean up Whisper word timings.

Sends the joined word stream to the model with a strict prompt that fixes:
 - Proper-noun spellings (driven by the user-supplied glossary/context)
 - Punctuation (colons after structural markers like "Year 8:", quote handling)
 - Capitalization of names

Returns word_timings re-aligned to the corrected text:
 - If the LLM's word count matches the input, timings are paired 1:1.
 - If the LLM adds or removes words, we LCS-align matching tokens and
   redistribute timings over inserted runs.

On any failure (network, parse, model unreachable) we return the original
word_timings unchanged so the workflow stays runnable.
"""

import json
import logging
import os
import re
import urllib.error
import urllib.request

logger = logging.getLogger("Ordo")


DEFAULT_SYSTEM_PROMPT = (
    "You are a transcription cleanup assistant. Your task is to fix a verbatim "
    "speech-to-text transcript so it reads correctly without changing what was said.\n"
    "\n"
    "RULES (apply in order):\n"
    "1. Preserve every spoken word. Do not delete content. Do not add new content.\n"
    "2. Fix proper-noun spelling using the glossary provided. Names listed in the "
    "glossary always use the glossary's exact spelling and casing.\n"
    "3. Fix punctuation that the speech recognizer missed:\n"
    "   - Add a colon after structural markers ('Year 8:', 'Chapter 3:', 'Day 2:').\n"
    "   - Capitalize the first letter of proper nouns and sentence starts.\n"
    "   - Add commas only where a clear pause demands one.\n"
    "4. Do not change word order. Do not change meaning. Do not add commentary.\n"
    "5. Output ONLY the corrected text. No quotes, no preamble, no explanation."
)


class OrdoLLMRefine:
    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                "word_timings": ("ORDO_WORD_TIMINGS",),
                "endpoint": ("STRING", {
                    "default": "http://model-gateway:11435/v1/chat/completions",
                    "tooltip": "OpenAI-compatible chat completions endpoint. Default points at the Ordo model-gateway.",
                }),
                "model": ("STRING", {
                    "default": "local-chat",
                    "tooltip": "Model name/alias the gateway routes to. Default 'local-chat' is a stable alias — the underlying local model can be swapped (currently Qwen 3.6) without editing this widget.",
                }),
                "api_key": ("STRING", {
                    "default": "local",
                    "tooltip": "Bearer token for the gateway. On this stack the LITELLM_MASTER_KEY is 'local'. Set to empty to inherit from OPENAI_API_KEY env if present.",
                }),
                "glossary": ("STRING", {
                    "multiline": True,
                    "default": (
                        "Solstryker\n"
                        "Targaryen\n"
                        "Westeros\n"
                        "Valyria\n"
                    ),
                    "tooltip": "One proper noun per line with the exact spelling/casing you want. Passed to the LLM as a glossary.",
                }),
                "style_notes": ("STRING", {
                    "multiline": True,
                    "default": (
                        "Use a colon after structural markers like 'Year 8:' or 'Chapter 3:'.\n"
                        "Capitalize House names ('House Black')."
                    ),
                    "tooltip": "Extra style instructions appended to the system prompt.",
                }),
            },
            "optional": {
                "wake_llamacpp": ("BOOLEAN", {
                    "default": False,
                    "tooltip": "RETIRED no-op, kept so saved workflows still load. The ops-api guardian this drove is gone: GPU arbitration is the ops-controller scheduler's, and while ComfyUI holds residency llama.cpp is evicted deliberately. Starting it back would put the LLM on the card beside this render. No wake is needed — the model-gateway fails 'local-chat' over to llamacpp-cpu automatically, so the call below just works (slower).",
                }),
                "ops_controller_url": ("STRING", {
                    "default": "http://ops-controller:9000",
                    "tooltip": "ops-controller base URL (inside the docker network).",
                }),
                "ops_controller_token": ("STRING", {
                    "default": "",
                    "tooltip": "Bearer token for ops-controller. Empty = read from OPS_CONTROLLER_TOKEN env in the comfyui container.",
                }),
                "temperature": ("FLOAT", {"default": 0.0, "min": 0.0, "max": 1.0, "step": 0.05}),
                "max_tokens": ("INT", {"default": 16384, "min": 256, "max": 32768, "step": 256,
                                       "tooltip": "Reasoning models (Qwen 3, etc.) burn most tokens in <think>; budget generously."}),
                "timeout_seconds": ("INT", {"default": 180, "min": 10, "max": 600, "step": 10}),
                "wake_timeout_seconds": ("INT", {"default": 90, "min": 10, "max": 300, "step": 10,
                                                  "tooltip": "Max seconds to wait for llamacpp to come back after a wake."}),
                "system_prompt_override": ("STRING", {
                    "multiline": True,
                    "default": "",
                    "tooltip": "Optional full replacement for the system prompt. Leave empty to use the default."
                }),
                "bypass": ("BOOLEAN", {
                    "default": False,
                    "tooltip": "True = skip the LLM call and pass word_timings through unchanged.",
                }),
            },
        }

    RETURN_TYPES = ("ORDO_WORD_TIMINGS",)
    RETURN_NAMES = ("word_timings",)
    FUNCTION = "refine"
    CATEGORY = "Ordo/LastMile"
    DESCRIPTION = "Polish Whisper output via a local LLM — fixes proper nouns, punctuation, casing while preserving timings."

    # ------------------------------------------------------------------ helpers

    def _call_llm(self, endpoint, model, system, user, temperature, max_tokens, timeout, api_key=""):
        api_key = (
            (api_key or "").strip()
            or os.environ.get("OPENAI_API_KEY")
            or os.environ.get("MODEL_GATEWAY_KEY")
            or os.environ.get("LITELLM_MASTER_KEY")
            or "local"  # matches the default LITELLM_MASTER_KEY on this stack
        )
        payload = {
            "model": model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "temperature": float(temperature),
            "max_tokens": int(max_tokens),
            "stream": False,
        }
        req = urllib.request.Request(
            endpoint,
            data=json.dumps(payload).encode("utf-8"),
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {api_key}",
            },
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=timeout) as r:
            body = json.load(r)
        content = body["choices"][0]["message"]["content"]
        return content.strip()

    @staticmethod
    def _tokenize_for_align(text: str) -> list[str]:
        """Split corrected text into tokens that line up with Whisper's word_timings.

        Whisper emits each word separately, with attached trailing punctuation
        (e.g. "flew.", "8:"). To realign, we mirror that: tokens are whitespace-
        separated, no per-token cleaning here.
        """
        return [t for t in re.split(r"\s+", text.strip()) if t]

    @staticmethod
    def _key(w: str) -> str:
        """Normalize a word for alignment matching."""
        return re.sub(r"[^a-z0-9]+", "", w.lower())

    def _realign(self, original: list[dict], corrected_tokens: list[str]) -> list[dict]:
        """Map corrected_tokens back onto original timings.

        Strategy:
        - If counts match exactly, pair-wise.
        - Else, longest-common-subsequence over normalized word keys. Insertions
          in the corrected stream get their time range estimated from the
          surrounding anchor words; deletions in the original drop those timings.
        """
        if len(original) == len(corrected_tokens):
            return [
                {**o, "word": new}
                for o, new in zip(original, corrected_tokens)
            ]

        # LCS alignment
        o_keys = [self._key(w["word"]) for w in original]
        c_keys = [self._key(t) for t in corrected_tokens]
        n, m = len(o_keys), len(c_keys)
        dp = [[0] * (m + 1) for _ in range(n + 1)]
        for i in range(n - 1, -1, -1):
            for j in range(m - 1, -1, -1):
                if o_keys[i] == c_keys[j]:
                    dp[i][j] = dp[i + 1][j + 1] + 1
                else:
                    dp[i][j] = max(dp[i + 1][j], dp[i][j + 1])

        # Walk to produce edit script
        ops: list[tuple[str, int, int]] = []  # ('match', oi, ci) | ('ins', _, ci) | ('del', oi, _)
        i = j = 0
        while i < n and j < m:
            if o_keys[i] == c_keys[j]:
                ops.append(("match", i, j))
                i += 1
                j += 1
            elif dp[i + 1][j] >= dp[i][j + 1]:
                ops.append(("del", i, -1))
                i += 1
            else:
                ops.append(("ins", -1, j))
                j += 1
        while i < n:
            ops.append(("del", i, -1))
            i += 1
        while j < m:
            ops.append(("ins", -1, j))
            j += 1

        # Group consecutive inserts so they share a time span
        out: list[dict] = []
        pending_ins: list[int] = []

        def flush_inserts(anchor_start: float, anchor_end: float, seg_idx: int):
            if not pending_ins:
                return
            span = max(anchor_end - anchor_start, 1e-3)
            step = span / len(pending_ins)
            for k, ci in enumerate(pending_ins):
                out.append({
                    "word": corrected_tokens[ci],
                    "start": anchor_start + k * step,
                    "end": anchor_start + (k + 1) * step,
                    "segment_idx": seg_idx,
                })
            pending_ins.clear()

        # Determine anchor times for inserts at boundaries
        for idx, op in enumerate(ops):
            kind = op[0]
            if kind == "match":
                oi, ci = op[1], op[2]
                # Inserts pending before this match — span the gap to this match
                anchor_start = out[-1]["end"] if out else original[oi]["start"] - 0.05
                anchor_end = original[oi]["start"]
                flush_inserts(anchor_start, anchor_end, original[oi].get("segment_idx", -1))
                # Emit the matched corrected token with original timing
                out.append({**original[oi], "word": corrected_tokens[ci]})
            elif kind == "ins":
                pending_ins.append(op[2])
            elif kind == "del":
                # Skip — the original word was removed by the LLM
                pass

        # Trailing inserts
        if pending_ins:
            anchor_start = out[-1]["end"] if out else 0.0
            anchor_end = original[-1]["end"] if original else anchor_start + 0.5
            flush_inserts(anchor_start, anchor_end, (original[-1].get("segment_idx", -1) if original else -1))

        return out

    # ------------------------------------------------------------------ main

    def refine(self, word_timings,
               endpoint="http://model-gateway:11435/v1/chat/completions",
               model="local-chat",
               api_key="local",
               glossary="Solstryker\nTargaryen\nWesteros\nValyria\n",
               style_notes="Use a colon after structural markers like 'Year 8:' or 'Chapter 3:'.\nCapitalize House names ('House Black').",
               wake_llamacpp=True,
               ops_controller_url="http://ops-controller:9000",
               ops_controller_token="",
               temperature=0.0, max_tokens=16384, timeout_seconds=180,
               wake_timeout_seconds=90,
               system_prompt_override="",
               bypass=False):
        if bypass:
            return (list(word_timings),)
        if not word_timings:
            return ([],)

        # NO WAKE. While this prompt runs, ComfyUI holds GPU residency and the scheduler has
        # deliberately evicted llama.cpp from the card; asking for it back would run the LLM
        # beside this render, which is the dual-tenancy that bugchecked the host on 2026-08-08.
        # The LLM has not gone away — it migrated: the model-gateway fails 'local-chat' over to
        # llamacpp-cpu (same model, same 131072 window, slower) and routes back on its own once
        # the GPU model is healthy. So the plain call below is correct and needs no help.
        if wake_llamacpp:
            logger.info(
                "[LLMRefine] wake_llamacpp is a retired no-op — GPU arbitration belongs to the "
                "ops-controller scheduler; 'local-chat' is already failing over to llamacpp-cpu."
            )

        joined = " ".join(w["word"] for w in word_timings)

        system = system_prompt_override.strip() or DEFAULT_SYSTEM_PROMPT
        if style_notes.strip():
            system = system + "\n\nADDITIONAL STYLE NOTES:\n" + style_notes.strip()

        glossary_lines = [g.strip() for g in (glossary or "").splitlines() if g.strip() and not g.strip().startswith("#")]
        glossary_block = ""
        if glossary_lines:
            glossary_block = "GLOSSARY (exact spelling/casing):\n" + "\n".join(f"- {g}" for g in glossary_lines)

        user_msg = (
            (glossary_block + "\n\n" if glossary_block else "")
            + "TRANSCRIPT TO CORRECT:\n"
            + joined
        )

        try:
            corrected = self._call_llm(endpoint, model, system, user_msg,
                                       temperature, max_tokens, timeout_seconds,
                                       api_key=api_key)
        except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, OSError) as e:
            logger.warning(f"[LLMRefine] LLM call failed ({type(e).__name__}: {e}). Passing input through unchanged.")
            return (list(word_timings),)
        except (KeyError, ValueError, json.JSONDecodeError) as e:
            logger.warning(f"[LLMRefine] LLM response parse failed ({type(e).__name__}: {e}). Passing input through.")
            return (list(word_timings),)

        if not corrected:
            logger.warning(
                "[LLMRefine] LLM returned empty content (reasoning model likely ran "
                "out of max_tokens during <think>). Bump max_tokens or set wake_timeout "
                "higher. Passing input through."
            )
            return (list(word_timings),)

        corrected_tokens = self._tokenize_for_align(corrected)
        if not corrected_tokens:
            return (list(word_timings),)

        out = self._realign(word_timings, corrected_tokens)
        logger.info(
            f"[LLMRefine] {len(word_timings)} words in -> {len(out)} words out "
            f"(LLM produced {len(corrected_tokens)} tokens)"
        )
        return (out,)
