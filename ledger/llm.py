"""Headless model calls via `omp -p --mode json`, with full provenance.

Every call records: stage, prompt template + its hash (prompt version), the
full rendered input (stored blob), the raw event stream (stored blob), the
model requested AND the model that actually answered (provider fallback is
silent in text mode — observed 2026-09-27: a glm-5.3-flash request answered
by claude-sonnet-5 after a 429), tokens, cost, and outcome.

A stage declares `min_rank`; an answer from a lower-ranked model (e.g. the
auditor falling back from opus to glm-flash) is recorded but returned as a
failure, so a weak model can never silently pass a gate.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path

import yaml

from . import core

PROMPTS_DIR = core.PIPELINE_ROOT / "prompts"
CONFIG_PATH = core.PIPELINE_ROOT / "config.yaml"
SYSTEM_PROMPT = ("You are a precise function inside an automated pipeline. Follow the "
                 "instructions exactly and output only what is asked, with no preamble.")


def load_config() -> dict:
    return yaml.safe_load(CONFIG_PATH.read_text(encoding="utf-8"))


@dataclass
class LLMResult:
    ok: bool
    text: str | None
    call_id: int
    actual_model: str | None
    error: str | None = None
    fallback_errors: list = field(default_factory=list)

    def json(self):
        """Parse the first JSON object/array in the output (models sometimes
        wrap JSON in a code fence)."""
        return extract_json(self.text or "")


def extract_json(text: str):
    fence = re.search(r"```(?:json)?\s*(.*?)```", text, re.S)
    if fence:
        text = fence.group(1)
    text = text.strip()
    starts = [i for i in (text.find("{"), text.find("[")) if i >= 0]
    if not starts:
        raise ValueError("no JSON in model output")
    return json.JSONDecoder().raw_decode(text[min(starts):])[0]


def render(template: str, variables: dict) -> str:
    """{{name}} substitution; every placeholder must be supplied."""
    def sub(m):
        key = m.group(1)
        if key not in variables:
            raise KeyError(f"prompt variable {key!r} not supplied")
        v = variables[key]
        return v if isinstance(v, str) else json.dumps(v, ensure_ascii=False, indent=1)
    return re.sub(r"\{\{(\w+)\}\}", sub, template)


def _parse_stream(raw: str) -> dict:
    final, errors = None, []
    for line in raw.splitlines():
        try:
            ev = json.loads(line)
        except ValueError:
            continue
        if ev.get("type") != "message_end":
            continue
        msg = ev.get("message") or {}
        if msg.get("role") != "assistant":
            continue
        model = f"{msg.get('provider')}/{msg.get('model')}"
        if msg.get("stopReason") == "error":
            errors.append({"model": model, "status": msg.get("errorStatus"),
                           "error": (msg.get("errorMessage") or "")[:500]})
        else:
            final = msg
    if final is None:
        return {"text": None, "model": None, "usage": {}, "errors": errors}
    text = "".join(c.get("text", "") for c in final.get("content") or [] if c.get("type") == "text")
    return {"text": text, "model": f"{final.get('provider')}/{final.get('model')}",
            "usage": final.get("usage") or {}, "errors": errors}


def _run_omp(model: str, thinking: str | None, prompt: str, timeout: int):
    """One omp invocation. Returns (parsed, raw, err, duration_ms)."""
    # shutil.which resolves omp.exe/omp.cmd wrappers that a bare "omp" argv
    # would miss when subprocess runs without a shell on Windows.
    omp_bin = shutil.which("omp") or "omp"
    cmd = [omp_bin, "-p", "--mode", "json", "--model", model, "--no-tools", "--no-session",
           "--no-skills", "--no-rules", "--no-extensions", "--no-title",
           "--system-prompt", SYSTEM_PROMPT]
    if thinking:
        cmd += ["--thinking", thinking]

    fd, tmp = tempfile.mkstemp(suffix=".md", prefix="omp_")
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write(prompt)
    started = time.monotonic()
    err = None
    try:
        proc = subprocess.run(cmd + [f"@{tmp}"], capture_output=True, text=True,
                              encoding="utf-8", timeout=timeout, cwd=tempfile.gettempdir())
        raw = proc.stdout
        if proc.returncode != 0:
            err = f"omp exited {proc.returncode}: {proc.stderr[-500:]}"
    except subprocess.TimeoutExpired as e:
        raw = e.stdout if isinstance(e.stdout, str) else ""
        err = f"timeout after {timeout}s"
    finally:
        os.unlink(tmp)
    duration_ms = int((time.monotonic() - started) * 1000)
    parsed = _parse_stream(raw or "")
    if err is None and parsed["text"] is None:
        err = "no assistant answer (all models failed)"
    return parsed, raw, err, duration_ms


def call(stage: str, prompt_name: str, variables: dict, *, job_id: str | None = None,
         actor: str | None = None, timeout: int = 900, conn=None, start_at: int = 0) -> LLMResult:
    cfg = load_config()
    st = cfg["stages"][stage]
    ranks = cfg.get("model_ranks", {})
    template_path = PROMPTS_DIR / f"{prompt_name}.md"
    template = template_path.read_text(encoding="utf-8")
    prompt = render(template, variables)

    # Try the stage's model, then its configured fallbacks (long-running stages
    # fall back to a mid-tier model; the intelligence stage falls back to a
    # bigger one). Every attempt is gated by min_rank exactly like the primary,
    # so a weak fallback can never pass.
    parsed = raw = None
    duration_ms = 0
    err = None
    attempt_errors = []
    chain = [st["model"]] + list(st.get("fallbacks") or [])
    if 0 < start_at < len(chain):
        chain = chain[start_at:]  # escalate: skip the models that already failed this task's verification
    for i, model in enumerate(chain):
        parsed, raw, err, duration_ms = _run_omp(model, st.get("thinking"), prompt, timeout)
        actual = parsed["model"]
        if err is None and ranks.get(actual, 0) < st["min_rank"]:
            err = (f"answered by {actual} (rank {ranks.get(actual, 0)}) but stage {stage!r} "
                   f"requires rank >= {st['min_rank']}")
        if err is None:
            break
        attempt_errors.append({"model": model, "error": err})
        if i + 1 < len(chain):
            err = f"{err} -> falling back to {chain[i + 1]}"
    usage = parsed["usage"]
    cost = (usage.get("cost") or {}).get("total")

    own = conn is None
    conn = conn or core.connect()
    try:
        with core.tx(conn):
            input_sha = core.store_bytes(conn, prompt.encode("utf-8"), "md", actor or f"llm:{stage}", job_id)
            raw_sha = core.store_bytes(conn, (raw or "").encode("utf-8"), "jsonl",
                                       actor or f"llm:{stage}", job_id) if raw else None
            text = parsed["text"]
            call_id = core.record(conn, "llm_calls", {
                "stage": stage, "job_id": job_id, "prompt_name": prompt_name,
                "prompt_sha256": core.sha256_text(template),
                "requested_model": st["model"], "actual_model": actual, "min_rank": st["min_rank"],
                "fallback_errors": core.canonical(parsed["errors"] + attempt_errors),
                "input_sha256": input_sha, "raw_sha256": raw_sha,
                "output_text": text, "output_sha256": core.sha256_text(text) if text is not None else None,
                "tokens_in": (usage.get("input") or 0) + (usage.get("cacheRead") or 0) + (usage.get("cacheWrite") or 0),
                "tokens_out": usage.get("output"), "cost_usd": cost, "duration_ms": duration_ms,
                "ok": 0 if err else 1, "error": err, "created_at": core.now()},
                actor=actor or f"llm:{stage}", event_type="llm.call", job_id=job_id)
    finally:
        if own:
            conn.close()
    return LLMResult(ok=err is None, text=parsed["text"], call_id=call_id, actual_model=actual,
                     error=err, fallback_errors=parsed["errors"] + attempt_errors)
