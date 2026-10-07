#!/usr/bin/env python3
"""Measure one provider call's cost on a real workbook, printing numbers only.

Stdlib only, no store, no package change: this tool ingests and projects a
workbook with the package's own functions, calls the provider directly with
``urllib``, and resolves the answer in memory. Every byte it prints comes from
a fixed dictionary of numeric and closed-enum keys, so a label, an answer or a
response body cannot leak by construction. The API key is read only from an
environment variable, is never printed or placed in a URL, and any text in an
error is redacted before it reaches a stream.

Usage:
    python tools/live_probe.py <path.xlsx> --provider openai --model gpt-4o-mini
        [--base-url URL] [--env-var NAME] [--param k=v ...] [--tab N]
        [--repeat K] [--max-tokens N] [--workers N] [--no-temperature]
        [--tiny] [--dump PATH] [--prompt-variant NAME]
    python tools/live_probe.py --gold DIR --provider openai --model gpt-4o-mini
        [--contract json|lines] [--prompt-variant NAME] [--workers N]
        [--record-out PATH]
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import socket
import statistics
import sys
import tempfile
import time
import traceback
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

import score_gold

from formextract.coverage import compute_coverage
from formextract.ingest import ingest
from formextract.layout import analyze
from formextract.model import to_dict
from formextract.pipeline import PROMPT_VARIANTS, Pipeline, PipelineConfig, page_tabs_for
from formextract.resolve import (
    LLMResponse,
    ProjectionChunk,
    build_prompt,
    drafts_to_fields,
    parse_drafts_with_errors,
    project_chunks,
)
from formextract.store import Store

FINISH_CLASSES = ("stop", "length", "safety", "other", "none")
STATUS_CLASSES = ("complete", "partial", "failed")
HTTP_ERROR_CLASSES = ("none", "4xx", "5xx", "timeout", "other")

REQUEST_TIMEOUT = 120.0

DEFAULT_BASE_URL = {
    "openai": "https://api.openai.com/v1",
    "gemini": "https://generativelanguage.googleapis.com/v1beta",
}
DEFAULT_ENV_VAR = {
    "openai": "OPENAI_API_KEY",
    "gemini": "GEMINI_API_KEY",
}

TINY_PROMPT = 'Reply with exactly this JSON object and nothing else: {"ok": true}'

BASE_KEYS = (
    "chunks_run",
    "input_chars",
    "prompt_tokens",
    "output_tokens",
    "reasoning_tokens",
    "finish_reason_class",
    "hit_length_cap",
    "seconds",
    "tokens_per_second",
    "fields",
    "parse_errors",
    "truncated",
    "unresolved_refs",
    "anchors",
    "grid_units",
    "units_consumed",
    "units_total",
    "status_class",
    "http_error_class",
    "floor_seconds",
)
_AGG_SUFFIXES = ("_min", "_median", "_max", "_count")
ALLOWED_KEYS = frozenset(
    list(BASE_KEYS)
    + [key + suffix for key in BASE_KEYS for suffix in _AGG_SUFFIXES]
    + ["repeat", "truncation_count", "prompt_variant"]
)
ALLOWED_STRING_VALUES = frozenset(
    FINISH_CLASSES + STATUS_CLASSES + HTTP_ERROR_CLASSES + PROMPT_VARIANTS
)


class ProbeError(Exception):
    """A probe failure whose message is safe to print after redaction."""


# ---------------------------------------------------------------------------
# key safety
# ---------------------------------------------------------------------------
def redact(text: str, secrets: set[str]) -> str:
    """Replace every non-empty secret substring with a fixed placeholder."""
    out = text
    for secret in secrets:
        if secret:
            out = out.replace(secret, "<redacted>")
    return out


def render_error(
    exc: BaseException, secrets: set[str]
) -> tuple[str, str, str]:
    """Return (class name, redacted message, redacted traceback) for ``exc``."""
    return (
        type(exc).__name__,
        redact(str(exc), secrets),
        redact("".join(traceback.format_exception(exc)), secrets),
    )


def _secrets_for(provider: str, key: str) -> set[str]:
    secrets: set[str] = set()
    if key:
        secrets.add(key)
        if provider != "gemini":
            secrets.add("Bearer " + key)
    return secrets


# ---------------------------------------------------------------------------
# parameters
# ---------------------------------------------------------------------------
def _coerce(raw: str) -> Any:
    low = raw.lower()
    if low == "true":
        return True
    if low == "false":
        return False
    try:
        return int(raw)
    except ValueError:
        pass
    try:
        return float(raw)
    except ValueError:
        pass
    return raw


def parse_param(raw: str) -> tuple[str, Any]:
    """Argparse ``type`` for a repeatable ``--param k=v``."""
    if "=" not in raw:
        raise argparse.ArgumentTypeError(f"--param must be k=v, got {raw!r}")
    key, value = raw.split("=", 1)
    key = key.strip()
    if not key:
        raise argparse.ArgumentTypeError(f"--param needs a name, got {raw!r}")
    return key, _coerce(value)


def build_params(pairs: list[tuple[str, Any]], no_temperature: bool) -> dict[str, Any]:
    """The package default is ``{"temperature": 0}`` unless overridden."""
    params: dict[str, Any] = {}
    if not no_temperature:
        params["temperature"] = 0
    for key, value in pairs:
        params[key] = value
    return params


# ---------------------------------------------------------------------------
# provider transport
# ---------------------------------------------------------------------------
def _openai_url(base_url: str) -> str:
    return base_url.rstrip("/") + "/chat/completions"


def _gemini_url(base_url: str, model: str) -> str:
    name = model[len("models/"):] if model.startswith("models/") else model
    return base_url.rstrip("/") + "/models/" + name + ":generateContent"


def _headers(provider: str, key: str) -> dict[str, str]:
    headers = {"Content-Type": "application/json"}
    if key:
        if provider == "gemini":
            headers["x-goog-api-key"] = key
        else:
            headers["Authorization"] = "Bearer " + key
    return headers


def _openai_body(
    model: str, prompt: str, params: dict[str, Any], max_tokens: int | None
) -> dict[str, Any]:
    body: dict[str, Any] = {
        "model": model,
        "messages": [{"role": "user", "content": prompt}],
    }
    body.update(params)
    if max_tokens is not None:
        body["max_completion_tokens"] = int(max_tokens)
    return body


def _gemini_body(
    model: str, prompt: str, params: dict[str, Any], max_tokens: int | None
) -> dict[str, Any]:
    generation: dict[str, Any] = {}
    for key, value in params.items():
        if key == "thinkingBudget":
            generation.setdefault("thinkingConfig", {})["thinkingBudget"] = int(value)
        else:
            generation[key] = value
    if max_tokens is not None:
        generation["maxOutputTokens"] = int(max_tokens)
    body: dict[str, Any] = {"contents": [{"parts": [{"text": prompt}]}]}
    if generation:
        body["generationConfig"] = generation
    return body


def _classify_finish(finish_reason: Any) -> str:
    if finish_reason is None:
        return "none"
    value = str(finish_reason).lower()
    if value in ("stop", "end_turn"):
        return "stop"
    if value in ("length", "max_tokens"):
        return "length"
    if value in (
        "safety",
        "content_filter",
        "recitation",
        "blocklist",
        "prohibited_content",
    ):
        return "safety"
    return "other"


def _parse_openai(text: str) -> tuple[str, Any, Any, Any, Any]:
    data = json.loads(text)
    choices = data.get("choices") or []
    choice = choices[0] if choices else {}
    message = choice.get("message") or {}
    usage = data.get("usage") or {}
    details = usage.get("completion_tokens_details") or {}
    return (
        message.get("content") or "",
        choice.get("finish_reason"),
        usage.get("prompt_tokens"),
        usage.get("completion_tokens"),
        details.get("reasoning_tokens"),
    )


def _parse_gemini(text: str) -> tuple[str, Any, Any, Any, Any]:
    data = json.loads(text)
    candidates = data.get("candidates") or []
    candidate = candidates[0] if candidates else {}
    parts = (candidate.get("content") or {}).get("parts") or []
    content = "".join(part.get("text", "") for part in parts)
    usage = data.get("usageMetadata") or {}
    return (
        content,
        candidate.get("finishReason"),
        usage.get("promptTokenCount"),
        usage.get("candidatesTokenCount"),
        usage.get("thoughtsTokenCount"),
    )


def _http_error_class(status: int) -> str:
    if 400 <= status < 500:
        return "4xx"
    if 500 <= status < 600:
        return "5xx"
    return "other"


def _http_post(url: str, headers: dict[str, str], payload: dict[str, Any]) -> tuple[int, bytes]:
    data = json.dumps(payload).encode("utf-8")
    request = urllib.request.Request(url, data=data, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(request, timeout=REQUEST_TIMEOUT) as response:
            return response.status, response.read()
    except urllib.error.HTTPError as exc:
        # Never read the body: a provider error body can echo the key.
        return exc.code, b""


def call_provider(
    provider: str,
    base_url: str,
    key: str,
    model: str,
    prompt: str,
    params: dict[str, Any],
    max_tokens: int | None,
) -> dict[str, Any]:
    """One provider call: one request, no retry, no text outside this dict."""
    if provider == "gemini":
        url = _gemini_url(base_url, model)
        body = _gemini_body(model, prompt, params, max_tokens)
    else:
        url = _openai_url(base_url)
        body = _openai_body(model, prompt, params, max_tokens)
    result: dict[str, Any] = {
        "http_error": "none",
        "content": "",
        "finish_class": "none",
        "prompt_tokens": None,
        "output_tokens": None,
        "reasoning_tokens": None,
    }
    try:
        status, raw = _http_post(url, _headers(provider, key), body)
    except (socket.timeout, TimeoutError):
        result["http_error"] = "timeout"
        return result
    except urllib.error.URLError as exc:
        result["http_error"] = (
            "timeout"
            if isinstance(exc.reason, (socket.timeout, TimeoutError))
            else "other"
        )
        return result
    except Exception:  # noqa: BLE001 - any transport failure is classified
        result["http_error"] = "other"
        return result
    if status >= 400:
        result["http_error"] = _http_error_class(status)
        return result
    try:
        content, finish, prompt_tokens, output_tokens, reasoning_tokens = (
            _parse_gemini(raw.decode("utf-8", "replace"))
            if provider == "gemini"
            else _parse_openai(raw.decode("utf-8", "replace"))
        )
    except Exception:  # noqa: BLE001 - an unreadable body is an http error class
        result["http_error"] = "other"
        return result
    result["content"] = content
    result["finish_class"] = _classify_finish(finish)
    result["prompt_tokens"] = prompt_tokens
    result["output_tokens"] = output_tokens
    result["reasoning_tokens"] = reasoning_tokens
    return result


def _is_truncated(text: str) -> bool:
    """True when the response is an unterminated JSON object.

    A single string- and escape-aware scan; code fences are stripped first.
    Balanced JSON (valid or not) is never reported as truncated.
    """
    stripped = text.strip()
    if stripped.startswith("```"):
        stripped = stripped.strip("`")
        if stripped.startswith("json"):
            stripped = stripped[4:]
    start = stripped.find("{")
    if start == -1:
        return False
    depth = 0
    in_string = False
    escaped = False
    for char in stripped[start:]:
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            continue
        if char == '"':
            in_string = True
        elif char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return False
    return True


# ---------------------------------------------------------------------------
# row coverage (the same definition the package uses)
# ---------------------------------------------------------------------------
def coverage_counts(
    layout, elements_by_id: dict, fields: list, chunks: list[ProjectionChunk],
    *, tabs: list[str], page_tabs: dict[int, str]
) -> tuple[int, int, int, int]:
    """(anchors, grid units, units consumed, units total) for the chunks' tabs.

    Delegates to ``formextract.coverage.compute_coverage`` so the probe reads
    the same anchors + GRID-region denominator the pipeline reports, then keeps
    only the tabs the selected chunks cover.
    """
    blocks = compute_coverage(
        layout, elements_by_id, fields, page_tabs=page_tabs, tabs=tabs
    )
    selected = {chunk.key for chunk in chunks}
    blocks = [block for block in blocks if block.tab in selected]
    return (
        sum(block.anchors for block in blocks),
        sum(block.grid_regions for block in blocks),
        sum(block.units_consumed for block in blocks),
        sum(block.units_total for block in blocks),
    )


# ---------------------------------------------------------------------------
# one measured run
# ---------------------------------------------------------------------------
def run_once(
    *,
    provider: str,
    base_url: str,
    key: str,
    model: str,
    params: dict[str, Any],
    max_tokens: int | None,
    chunks: list[ProjectionChunk],
    layout,
    elements_by_id: dict,
    tabs: list[str],
    page_tabs: dict[int, str],
    workers: int,
    dump_entries: list | None,
) -> dict[str, Any]:
    prompts = [build_prompt(chunk, include_address=False) for chunk in chunks]

    def one(prompt: str) -> dict[str, Any]:
        return call_provider(provider, base_url, key, model, prompt, params, max_tokens)

    started = time.perf_counter()
    if workers <= 1:
        calls = [one(prompt) for prompt in prompts]
    else:
        with ThreadPoolExecutor(max_workers=workers) as executor:
            calls = list(executor.map(one, prompts))
    seconds = time.perf_counter() - started

    if dump_entries is not None:
        for prompt, call in zip(prompts, calls):
            dump_entries.append(
                {
                    "prompt": prompt,
                    "response": call["content"],
                    "http_error": call["http_error"],
                }
            )

    drafts: list = []
    parse_errors = 0
    truncated = False
    finish_class = "none"
    hit_length_cap = False
    http_error_class = "none"
    prompt_tokens = 0
    output_tokens = 0
    reasoning_tokens = 0
    saw_prompt_tokens = False
    saw_output_tokens = False
    saw_reasoning_tokens = False
    chunks_ok = 0

    for call in calls:
        if call["http_error"] != "none":
            if http_error_class == "none":
                http_error_class = call["http_error"]
            continue
        chunks_ok += 1
        if finish_class == "none" and call["finish_class"] != "none":
            finish_class = call["finish_class"]
        if call["finish_class"] == "length":
            hit_length_cap = True
        if call["prompt_tokens"] is not None:
            prompt_tokens += call["prompt_tokens"]
            saw_prompt_tokens = True
        if call["output_tokens"] is not None:
            output_tokens += call["output_tokens"]
            saw_output_tokens = True
        if call["reasoning_tokens"] is not None:
            reasoning_tokens += call["reasoning_tokens"]
            saw_reasoning_tokens = True
        # A length-truncated response is a parsed-but-flagged result now (the
        # solver salvages its complete fields), so detect the cut independently
        # of whether the parse raised.
        truncated = truncated or _is_truncated(call["content"])
        try:
            chunk_drafts, field_errors = parse_drafts_with_errors(
                call["content"], include_address=False
            )
        except ValueError:
            parse_errors += 1
            continue
        drafts.extend(chunk_drafts)
        parse_errors += len(field_errors)

    fields = drafts_to_fields(
        drafts,
        layout=layout,
        elements_by_id=elements_by_id,
        tabs=tabs,
        page_tabs=page_tabs,
    )
    unresolved_refs = sum(len(field.unresolved_source_refs) for field in fields)
    anchors, grid_units, units_consumed, units_total = coverage_counts(
        layout, elements_by_id, fields, chunks, tabs=tabs, page_tabs=page_tabs
    )

    chunks_run = len(chunks)
    if chunks_run and chunks_ok == 0:
        status_class = "failed"
    elif parse_errors or truncated or unresolved_refs or chunks_ok < chunks_run:
        status_class = "partial"
    else:
        status_class = "complete"

    return {
        "chunks_run": chunks_run,
        "input_chars": sum(len(prompt) for prompt in prompts),
        "prompt_tokens": prompt_tokens if saw_prompt_tokens else None,
        "output_tokens": output_tokens if saw_output_tokens else None,
        "reasoning_tokens": reasoning_tokens if saw_reasoning_tokens else None,
        "finish_reason_class": finish_class,
        "hit_length_cap": hit_length_cap,
        "seconds": seconds,
        "tokens_per_second": (
            output_tokens / seconds if seconds > 0 and saw_output_tokens else None
        ),
        "fields": len(fields),
        "parse_errors": parse_errors,
        "truncated": truncated,
        "unresolved_refs": unresolved_refs,
        "anchors": anchors,
        "grid_units": grid_units,
        "units_consumed": units_consumed,
        "units_total": units_total,
        "status_class": status_class,
        "http_error_class": http_error_class,
    }


def tiny_once(
    *,
    provider: str,
    base_url: str,
    key: str,
    model: str,
    params: dict[str, Any],
    max_tokens: int | None,
) -> dict[str, Any]:
    """A fixed tiny output run, to isolate the per-call floor ``F``."""
    started = time.perf_counter()
    call = call_provider(provider, base_url, key, model, TINY_PROMPT, params, max_tokens)
    seconds = time.perf_counter() - started
    return {
        "floor_seconds": seconds,
        "prompt_tokens": call["prompt_tokens"],
        "output_tokens": call["output_tokens"],
        "reasoning_tokens": call["reasoning_tokens"],
        "finish_reason_class": call["finish_class"],
        "hit_length_cap": call["finish_class"] == "length",
        "http_error_class": call["http_error"],
        "tokens_per_second": (
            call["output_tokens"] / seconds
            if seconds > 0 and call["output_tokens"] is not None
            else None
        ),
    }


def _is_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def aggregate(bases: list[dict[str, Any]], repeat: int) -> dict[str, Any]:
    """One JSON object for K runs: min/median/max per key, or the single run."""
    if repeat == 1:
        out = dict(bases[0])
        out["repeat"] = 1
        return out
    out: dict[str, Any] = {"repeat": repeat}
    for key, value in bases[0].items():
        values = [base[key] for base in bases]
        if key == "truncated":
            out["truncation_count"] = sum(1 for item in values if item)
        elif isinstance(value, bool):
            out[key + "_count"] = sum(1 for item in values if item)
        elif _is_number(value) or value is None:
            numbers = [item for item in values if item is not None]
            if numbers:
                out[key + "_min"] = min(numbers)
                out[key + "_median"] = statistics.median(numbers)
                out[key + "_max"] = max(numbers)
            else:
                out[key + "_min"] = None
                out[key + "_median"] = None
                out[key + "_max"] = None
        else:
            out[key] = value
    return out


def _write_dump(path: str, entries: list) -> None:
    with open(path, "w", encoding="utf-8") as handle:
        json.dump({"calls": entries}, handle, ensure_ascii=False, indent=2)
    print(
        f"warning: --dump wrote prompt and response text to {path}; "
        "it may contain private data",
        file=sys.stderr,
    )


# ---------------------------------------------------------------------------
# gold mode: run every gold tab through the real Pipeline, then score it
# ---------------------------------------------------------------------------
class GoldInputError(Exception):
    """A ``--gold`` directory that cannot be used (exit code 2)."""


class _ProviderError(Exception):
    """A provider failure raised out of the probe's gold client.

    Carries the HTTP class; a 4xx is given a status code so the pipeline's retry
    loop makes exactly one attempt for it, like any other non-retryable error.
    """

    _STATUS = {"4xx": 400, "5xx": 500}

    def __init__(self, http_error: str) -> None:
        super().__init__(f"provider error: {http_error}")
        self.http_error = http_error
        self.status_code = self._STATUS.get(http_error)


class _ProviderClient:
    """The probe's provider-backed ``LLMClient`` plus its call accounting."""

    def __init__(self, *, provider, base_url, key, max_tokens):
        self.provider = provider
        self.base_url = base_url
        self.key = key
        self.max_tokens = max_tokens
        self.calls = 0
        self.prompt_tokens = 0
        self.output_tokens = 0
        self.reasoning_tokens = 0
        self.saw_prompt = False
        self.saw_output = False
        self.saw_reasoning = False
        self.finish_counts = {name: 0 for name in FINISH_CLASSES}
        self.truncated = 0
        self.http_error_class = "none"

    def complete(
        self, prompt: str, *, model: str, params: dict[str, Any]
    ) -> LLMResponse:
        call = call_provider(
            self.provider, self.base_url, self.key, model, prompt, params, self.max_tokens
        )
        self.calls += 1
        self.finish_counts[call["finish_class"]] += 1
        if call["prompt_tokens"] is not None:
            self.prompt_tokens += call["prompt_tokens"]
            self.saw_prompt = True
        if call["output_tokens"] is not None:
            self.output_tokens += call["output_tokens"]
            self.saw_output = True
        if call["reasoning_tokens"] is not None:
            self.reasoning_tokens += call["reasoning_tokens"]
            self.saw_reasoning = True
        if _is_truncated(call["content"]):
            self.truncated += 1
        if call["http_error"] != "none":
            if self.http_error_class == "none":
                self.http_error_class = call["http_error"]
            raise _ProviderError(call["http_error"])
        return LLMResponse(
            text=call["content"],
            model=model,
            params=params,
            tokens=call["output_tokens"],
            latency_ms=None,
            finish_reason=call["finish_class"],
        )


def _load_gold_dir(gold_dir: Path) -> tuple[dict, dict]:
    if not gold_dir.is_dir():
        raise GoldInputError(f"--gold is not a directory: {gold_dir}")
    golds = sorted(
        p for p in gold_dir.glob("gold_*.json") if not p.name.startswith("gold_manifest_")
    )
    manifests = sorted(gold_dir.glob("gold_manifest_*.json"))
    if len(golds) != 1 or len(manifests) != 1:
        raise GoldInputError(
            "a --gold directory must hold one gold_*.json and one gold_manifest_*.json"
        )
    try:
        gold_doc = json.loads(golds[0].read_text(encoding="utf-8"))
        manifest = json.loads(manifests[0].read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise GoldInputError(f"cannot read the gold directory: {exc}") from exc
    if not isinstance(gold_doc.get("tabs"), list) or not isinstance(
        manifest.get("tab_sha256"), dict
    ):
        raise GoldInputError("the gold directory is missing its tabs or tab_sha256")
    return gold_doc, manifest


def _verify_gold_manifest(gold_dir: Path, gold_doc: dict, manifest: dict) -> None:
    """Refuse a gold directory whose tabs no longer match the manifest.

    The message never names a sheet, so the failure stays numbers/tags only.
    """
    for tab in gold_doc["tabs"]:
        path = gold_dir / f"{tab}.xlsx"
        if not path.is_file():
            raise GoldInputError("a gold tab is missing its .xlsx")
        actual = hashlib.sha256(path.read_bytes()).hexdigest()
        if actual != manifest["tab_sha256"].get(tab):
            raise GoldInputError("a gold tab does not match the manifest sha256")


def _score_gold(gold_doc: dict, dump: dict) -> dict:
    gold = score_gold.Gold(gold_doc)
    predicted, stray, unresolved = score_gold._predicted_fields(
        gold, score_gold._extract_fields(dump)
    )
    disposition = score_gold._disposition_accuracy(gold, dump)
    return score_gold.score_document(gold, predicted, stray, unresolved, disposition)


def run_gold_once(
    *, gold_dir, provider, base_url, key, model, params, max_tokens, workers,
    contract="json", prompt_variant="base",
) -> tuple[dict, dict, "_ProviderClient", float]:
    """Run every gold tab through the real Pipeline.

    A throwaway Store in a temp dir (nothing archived under the repo, the temp
    store is deleted), the gold's own conventions declared, ``workers`` as
    ``chunk_workers``, ``contract`` as ``output_contract`` (``json`` or
    ``lines``, so one gold set can be measured under both) and ``prompt_variant``
    as the lines contract's prompt variant (``base`` by default). Returns
    ``(dump, gold_doc, client, seconds)`` where ``dump`` is one record dump of
    every tab's fields.
    """
    gold_dir = Path(gold_dir)
    gold_doc, manifest = _load_gold_dir(gold_dir)
    _verify_gold_manifest(gold_dir, gold_doc, manifest)
    config = PipelineConfig(
        model=model,
        params=params,
        checkbox_conventions=list(gold_doc.get("checkbox_conventions") or []),
        chunk_workers=workers,
        output_contract=contract,
        prompt_variant=prompt_variant,
    )
    client = _ProviderClient(
        provider=provider, base_url=base_url, key=key, max_tokens=max_tokens
    )
    fields: list = []
    workdir = Path(tempfile.mkdtemp(prefix="probe-gold-"))
    started = time.perf_counter()
    try:
        store = Store(workdir)
        for tab in gold_doc["tabs"]:
            record = Pipeline(store, client, config).run(gold_dir / f"{tab}.xlsx")
            fields.extend(record.fields)
    finally:
        shutil.rmtree(workdir, ignore_errors=True)
    seconds = time.perf_counter() - started
    return {"fields": [to_dict(field) for field in fields]}, gold_doc, client, seconds


def _gold_output(
    gold_doc: dict,
    score: dict,
    client: "_ProviderClient",
    seconds: float,
    prompt_variant: str,
) -> dict:
    o = score["overall"]
    per_tag = {
        tag: {"matched": t["matched"], "gold": t["gold_fields"]}
        for tag, t in score["per_tag"].items()
    }
    return {
        "prompt_variant": prompt_variant,
        "gold_tabs": len(gold_doc["tabs"]),
        "calls": client.calls,
        "seconds": seconds,
        "prompt_tokens": client.prompt_tokens if client.saw_prompt else None,
        "output_tokens": client.output_tokens if client.saw_output else None,
        "reasoning_tokens": client.reasoning_tokens if client.saw_reasoning else None,
        "finish_reason_class_counts": dict(client.finish_counts),
        "truncated_count": client.truncated,
        "http_error_class": client.http_error_class,
        "gold_fields": o["gold_fields"],
        "predicted_fields": o["predicted_fields"],
        "matched": o["matched"],
        "precision_num": o["precision_num"],
        "precision_den": o["precision_den"],
        "recall_num": o["recall_num"],
        "recall_den": o["recall_den"],
        "precision": o["precision"],
        "recall": o["recall"],
        "merge_count": o["merge_count"],
        "split_count": o["split_count"],
        "missed": o["missed"],
        "spurious": o["spurious"],
        "stray_ref_count": o["stray_ref_count"],
        "unresolved_ref_count": o["unresolved_ref_count"],
        "label_ok": o["label_ok"],
        "label_total": o["label_total"],
        "options_ok": o["options_ok"],
        "options_total": o["options_total"],
        "selected_ok": o["selected_ok"],
        "selected_total": o["selected_total"],
        "selected_ok_under_convention": o["selected_ok_under_convention"],
        "selected_ambiguous_expected": o["selected_ambiguous_expected"],
        "unexpected_ambiguous": o["unexpected_ambiguous"],
        "answer_ok": o["answer_ok"],
        "answer_total": o["answer_total"],
        "addressed_num": o["addressed_num"],
        "addressed_den": o["addressed_den"],
        "per_tag": per_tag,
    }


def _execute_gold(args, key: str, base_url: str, params: dict[str, Any]) -> dict[str, Any]:
    dump, gold_doc, client, seconds = run_gold_once(
        gold_dir=args.gold,
        provider=args.provider,
        base_url=base_url,
        key=key,
        model=args.model,
        params=params,
        max_tokens=args.max_tokens,
        workers=args.workers,
        contract=args.contract,
        prompt_variant=args.prompt_variant,
    )
    if args.record_out:
        with open(args.record_out, "w", encoding="utf-8") as handle:
            json.dump(dump, handle, ensure_ascii=False, indent=2)
    return _gold_output(
        gold_doc, _score_gold(gold_doc, dump), client, seconds, args.prompt_variant
    )


# ---------------------------------------------------------------------------
# entry point
# ---------------------------------------------------------------------------
def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Measure one provider call's cost on a workbook; numbers only"
    )
    parser.add_argument(
        "path",
        nargs="?",
        help="path to a .xlsx/.pdf workbook (not needed with --tiny)",
    )
    parser.add_argument("--provider", choices=("openai", "gemini"), default="openai")
    parser.add_argument(
        "--base-url",
        default=None,
        help="any OpenAI-compatible base URL (openai) or Gemini base URL",
    )
    parser.add_argument("--model", required=True)
    parser.add_argument(
        "--env-var",
        default=None,
        help="environment variable holding the key; an empty value means no auth",
    )
    parser.add_argument(
        "--param",
        action="append",
        default=[],
        type=parse_param,
        metavar="K=V",
        help="repeatable provider parameter (int, float, bool, else str)",
    )
    parser.add_argument(
        "--tab", type=int, default=None, help="tab index among the chunks (never a name)"
    )
    parser.add_argument("--repeat", type=int, default=1)
    parser.add_argument("--max-tokens", type=int, default=None)
    parser.add_argument("--workers", type=int, default=1)
    parser.add_argument(
        "--no-temperature",
        action="store_true",
        help="omit temperature (for reasoning models that reject it)",
    )
    parser.add_argument(
        "--tiny",
        action="store_true",
        help="send a fixed tiny prompt and report floor_seconds",
    )
    parser.add_argument(
        "--gold",
        default=None,
        metavar="DIR",
        help="run every tab of a make_gold --out directory through the real "
        "Pipeline with the gold's own conventions and print one line of numbers",
    )
    parser.add_argument(
        "--contract",
        choices=("json", "lines"),
        default="json",
        help="output contract to run --gold under: the json baseline or the "
        "0.6.0 row lines",
    )
    parser.add_argument(
        "--prompt-variant",
        choices=PROMPT_VARIANTS,
        default="base",
        help="the lines contract's prompt variant (L3a); ignored by the json "
        "contract, which has its own prompt. Printed in the numbers output.",
    )
    parser.add_argument(
        "--record-out",
        default=None,
        metavar="PATH",
        help="write the combined record dump here (--gold only; off by default)",
    )
    parser.add_argument(
        "--dump",
        default=None,
        help="the one path prompt/response text may be written to",
    )
    return parser


def _execute(args, key: str, base_url: str, params: dict[str, Any]) -> dict[str, Any]:
    run_kwargs = {
        "provider": args.provider,
        "base_url": base_url,
        "key": key,
        "model": args.model,
        "params": params,
        "max_tokens": args.max_tokens,
    }
    if args.tiny:
        bases = [tiny_once(**run_kwargs) for _ in range(args.repeat)]
        output = aggregate(bases, args.repeat)
        output["prompt_variant"] = args.prompt_variant
        return output

    ing = ingest(args.path)
    elements = ing.elements
    hidden_names = {
        name for name, state in ing.sheet_state.items() if state != "visible"
    }
    if ing.sheet_state and hidden_names:
        elements = [e for e in elements if e.sheet not in hidden_names]
    if ing.sheet_state:
        tabs = [name for name in ing.sheet_names if name not in hidden_names]
    else:
        tabs = ing.sheet_names or [
            f"page {index}" for index in range(ing.page_count or 0)
        ]
    page_tabs = page_tabs_for(ing)
    layout = analyze(elements)
    chunks = project_chunks(layout, elements, tabs, page_tabs=page_tabs)

    if args.tab is not None:
        if args.tab < 0 or args.tab >= len(chunks):
            raise ProbeError(
                f"--tab {args.tab} is out of range (0..{max(len(chunks) - 1, 0)})"
            )
        selected = [chunks[args.tab]]
    else:
        selected = chunks

    elements_by_id = {element.element_id: element for element in elements}
    dump_entries: list | None = [] if args.dump else None
    bases = [
        run_once(
            chunks=selected,
            layout=layout,
            elements_by_id=elements_by_id,
            tabs=tabs,
            page_tabs=page_tabs,
            workers=args.workers,
            dump_entries=dump_entries,
            **run_kwargs,
        )
        for _ in range(args.repeat)
    ]
    if args.dump:
        _write_dump(args.dump, dump_entries or [])
    output = aggregate(bases, args.repeat)
    # The single-tab path measures the json prompt, which has no variants; the
    # name is closed and printed anyway so one invocation shape reports it.
    output["prompt_variant"] = args.prompt_variant
    return output


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if not args.tiny and args.gold is None and args.path is None:
        parser.error("the workbook path is required unless --tiny or --gold is given")
    if args.repeat < 1:
        parser.error("--repeat must be >= 1")
    if args.workers < 1:
        parser.error("--workers must be >= 1")

    name = args.env_var if args.env_var is not None else DEFAULT_ENV_VAR[args.provider]
    if name == "":
        key = ""
    elif name not in os.environ:
        print(f"key variable {name} is not set", file=sys.stderr)
        return 2
    else:
        key = os.environ[name]

    secrets = _secrets_for(args.provider, key)
    base_url = args.base_url or DEFAULT_BASE_URL[args.provider]
    params = build_params(args.param, args.no_temperature)

    try:
        if args.gold is not None:
            output = _execute_gold(args, key, base_url, params)
        else:
            output = _execute(args, key, base_url, params)
    except GoldInputError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    except Exception as exc:  # noqa: BLE001 - report a class, never a body
        class_name, message, _traceback_text = render_error(exc, secrets)
        print(f"error: {class_name}: {message}", file=sys.stderr)
        return 1

    print(json.dumps(output, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
