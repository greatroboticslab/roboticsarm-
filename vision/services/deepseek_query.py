"""
[TEMPORARY — local-LLM proof of concept, not the end state]
=============================================================

WHY THIS FILE EXISTS
---------------------
Lets the "Database" tab in main.py accept a plain-English question
("red objects from yesterday") and turn it into a MongoDB filter, run
against the local "objects" collection via vision.storage.mongo_client.

This is deliberately a thin, swappable layer:
  - The "known fields" it shows the model come from
    mongo_client.sample_recent_data_fields() — i.e. whatever keys
    already happen to be in recent samples' "data" dicts (manually
    typed today, since there's no classifier writing structured fields
    yet). Nothing here assumes a fixed schema.
  - If/when a classifier starts filling in fields automatically, or
    this gets handed off to 4DAI's server side instead, only
    mongo_client.sample_recent_data_fields() (or its caller) needs to
    change — generate_mongo_filter() and run_nl_query() below don't
    care where the field list came from.

WHY IT'S MARKED TEMPORARY
---------------------------
This talks to a local Ollama install (https://ollama.com) purely
because it's the fastest way to get a local DeepSeek model running
with zero cloud dependency/API key. It is NOT wired into any
production path, has no retry/queueing, and assumes Ollama is running
on localhost. Treat this as a proof of concept for "can NL -> Mongo
filter work at all locally" — swap it out (or harden it: timeouts,
retries, a real model-serving setup) before relying on it for
anything beyond manual browsing in the Database tab.

SETUP
-----
    1. Install Ollama: https://ollama.com
    2. Pull a model, e.g.:
           ollama pull deepseek-r1:7b
       or, for a lighter/faster option:
           ollama pull deepseek-r1:1.5b
    3. Make sure `ollama serve` is running (it runs automatically after
       install on most platforms) — this module expects
       vision.config.OLLAMA_HOST (default http://localhost:11434) to be
       reachable.

None of this is required for the rest of the app to work — every
function here fails soft (raises a caught, displayable exception) so
main.py can just disable the "Ask" button / show an error instead of
crashing when Ollama isn't running or the model isn't pulled.
"""

from __future__ import annotations

import json
import re

import requests

from vision.config import OLLAMA_HOST, OLLAMA_MODEL, NL_QUERY_FIELD_SAMPLE_SIZE
from vision.storage.mongo_client import find_samples, sample_recent_data_fields

# Kept for reference — the operator whitelist now lives in
# vision.storage.query_safety, shared with the Database tab's manual
# "Advanced Query" box. DeepSeekQueryError stays a distinct exception
# type since this module's callers (if any remain) expect it.
from vision.storage.query_safety import ALLOWED_OPERATORS as _ALLOWED_OPERATORS, \
    validate_filter as _shared_validate_filter

# deepseek-r1 is a reasoning model: it spends a lot of time generating an
# internal <think>...</think> block before producing the actual answer.
# That phase alone can run well past 30s on CPU, especially for the 7b
# variant -- 30s was cutting off legitimate, still-in-progress requests,
# not just genuinely hung ones. 120s gives the thinking phase room while
# still failing (rather than hanging forever) if Ollama is actually stuck.
_OLLAMA_TIMEOUT_SECONDS = 120


class DeepSeekQueryError(Exception):
    """Raised for anything that should be shown to the user as a plain
    error message rather than crashing the GUI — Ollama unreachable,
    model not pulled, unparseable/unsafe model output, etc."""


def check_ollama_available(model: str = OLLAMA_MODEL) -> None:
    """
    Raises DeepSeekQueryError with a human-readable reason if Ollama
    isn't reachable or the requested model hasn't been pulled. Call this
    before enabling any NL-query UI so it can be disabled instead of
    failing on first use.
    """
    try:
        resp = requests.get(f"{OLLAMA_HOST}/api/tags", timeout=5)
        resp.raise_for_status()
    except requests.RequestException as e:
        raise DeepSeekQueryError(
            f"Can't reach Ollama at {OLLAMA_HOST}: {e}\n"
            f"Is `ollama serve` running?"
        )

    try:
        available = {m.get("name", "") for m in resp.json().get("models", [])}
    except (ValueError, AttributeError):
        available = set()

    # Ollama tags include a version suffix (e.g. "deepseek-r1:7b"); match
    # loosely so "deepseek-r1:7b" matches an installed "deepseek-r1:7b"
    # even if Ollama reports extra metadata after it.
    if available and not any(m.startswith(model) for m in available):
        raise DeepSeekQueryError(
            f"Model '{model}' isn't pulled in Ollama.\n"
            f"Run: ollama pull {model}"
        )


def _validate_filter(filter_obj) -> dict:
    """Recursively check that a parsed filter only uses whitelisted
    operators — delegates to vision.storage.query_safety, translating
    its generic QueryValidationError into this module's
    DeepSeekQueryError so existing callers' except clauses still work."""
    from vision.storage.query_safety import QueryValidationError
    try:
        return _shared_validate_filter(filter_obj)
    except QueryValidationError as e:
        raise DeepSeekQueryError(str(e))


def _repair_unquoted_operators(text: str) -> str:
    """
    Best-effort repair for a specific, observed failure mode: small local
    models frequently emit MongoDB-shell/JS-style operator keys —
    `{$gt: 10}` — instead of strict JSON's `{"$gt": 10}`. json.loads()
    rejects the former ("Expecting property name enclosed in double
    quotes"), even though it's an unambiguous, safe fix: wrap any bare
    `$operator` immediately after `{` or `,` in double quotes. This is
    intentionally narrow (only touches keys starting with "$", not
    quotes anywhere else in the text) so it can't accidentally mangle a
    legitimate string value.
    """
    return re.sub(r'([{,]\s*)(\$[A-Za-z]+)(\s*:)', r'\1"\2"\3', text)


def _strip_think_blocks(text: str) -> str:
    """Remove <think>...</think> reasoning blocks that reasoning models
    like deepseek-r1 emit before their actual answer. Without this, the
    JSON-extraction logic below can grab braces from INSIDE the model's
    reasoning (e.g. it reasoning out loud about what the filter should
    look like) instead of the real, final answer -- producing either a
    parse error or a filter that doesn't match what the model actually
    intended. If the model's response got cut off mid-think (e.g. by a
    slow generation), there may be no closing tag; in that case there's
    no usable answer left, so this returns an empty string rather than
    guessing."""
    if "<think>" in text:
        if "</think>" in text:
            return text.split("</think>", 1)[1].strip()
        return ""  # still inside <think> when generation stopped -- no answer yet
    return text.strip()


def _extract_json(raw_text: str) -> dict:
    """Best-effort extraction of a JSON object from a local model's
    response — small/reasoning models often wrap output in ```json
    fences or add explanation text despite being told not to."""
    text = _strip_think_blocks(raw_text)

    if not text:
        raise DeepSeekQueryError(
            "The model's response was still inside a <think> reasoning "
            "block with no answer after it (generation was likely cut "
            "off). Try again, or use a smaller/faster model — see "
            "OLLAMA_MODEL in vision/config.py."
        )

    # Strip ```json ... ``` or ``` ... ``` fences if present.
    fence_match = re.search(r"```(?:json)?\s*(.*?)\s*```", text, re.DOTALL)
    if fence_match:
        text = fence_match.group(1).strip()

    # Fall back to grabbing the first {...} block in case there's still
    # stray prose (e.g. reasoning-model "thinking" text) around it.
    if not text.startswith("{"):
        brace_match = re.search(r"\{.*\}", text, re.DOTALL)
        if brace_match:
            text = brace_match.group(0)

    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass  # try the operator-quoting repair below before giving up

    repaired = _repair_unquoted_operators(text)
    try:
        return json.loads(repaired)
    except json.JSONDecodeError as e:
        raise DeepSeekQueryError(
            f"Couldn't parse a JSON filter from the model's response.\n"
            f"Raw response: {raw_text}\n"
            f"Parse error: {e}"
        )


def generate_mongo_filter(question: str, fields: list, model: str = OLLAMA_MODEL) -> dict:
    """
    Sends `question` plus the known `fields` to the local Ollama model
    and returns a validated Mongo filter dict. Raises DeepSeekQueryError
    on any failure (unreachable, bad/unsafe output).
    """
    fields_desc = ", ".join(fields) if fields else "(no fields found yet — samples may not have been collected)"

    prompt = (
        "You translate a natural-language question into a MongoDB find() "
        "filter for a collection of object samples. Each document looks "
        'like {"_id": str, "date": "YYYY-MM-DD", "data": {...fields...}}.\n\n'
        f"Known fields inside \"data\": {fields_desc}\n\n"
        f"Question: {question}\n\n"
        "Respond with ONLY a single JSON object usable as a MongoDB filter "
        "(e.g. referencing fields as \"data.<field>\" and/or \"date\"). "
        "No explanation, no markdown fences, no extra text — JSON only. "
        "This must be STRICT JSON: every key, including operators, must "
        "be in double quotes — write {\"data.count\": {\"$gt\": 10}}, "
        "never {\"data.count\": {$gt: 10}}. "
        "Only use these operators if needed: "
        f"{', '.join(sorted(_ALLOWED_OPERATORS))}. "
        "Never include write operators."
    )

    try:
        resp = requests.post(
            f"{OLLAMA_HOST}/api/generate",
            json={
                "model": model,
                "prompt": prompt,
                "stream": False,
                # Caps response length. The answer itself is a small JSON
                # object; 800 tokens is generous headroom for the <think>
                # block too, but stops a stuck/rambling generation from
                # running indefinitely and guarantees the request finishes
                # (successfully or not) well inside _OLLAMA_TIMEOUT_SECONDS.
                "options": {"num_predict": 800},
            },
            timeout=_OLLAMA_TIMEOUT_SECONDS,
        )
        resp.raise_for_status()
    except requests.RequestException as e:
        raise DeepSeekQueryError(f"Ollama request failed: {e}")

    raw_text = resp.json().get("response", "")
    parsed = _extract_json(raw_text)
    return _validate_filter(parsed)


def run_nl_query(question: str, limit: int = 30, field_sample_size: int = NL_QUERY_FIELD_SAMPLE_SIZE,
                  model: str = OLLAMA_MODEL) -> tuple:
    """
    End-to-end: known fields -> model-generated filter -> validated ->
    run against the local "objects" collection.

    Returns (samples, filter_used) so the caller (main.py) can display
    both the results and the filter that was actually run.
    Raises DeepSeekQueryError on any failure along the way.
    """
    fields = sample_recent_data_fields(limit=field_sample_size)
    mongo_filter = generate_mongo_filter(question, fields, model=model)
    samples = find_samples(mongo_filter, limit=limit)
    return samples, mongo_filter
