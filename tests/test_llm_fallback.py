"""llm.call's explicit fallback chain (2026-10-01): when a stage's primary
model fails (crash, or an answer below min_rank), the next configured fallback
runs, and its answer is accepted only if it clears min_rank. A chain that all
fails records ok=0. No network: subprocess is faked with canned omp streams.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from ledger import core, llm  # noqa: E402


def _stream(provider_model):
    p, m = provider_model.split("/")
    line = json.dumps({"type": "message_end", "message": {
        "role": "assistant", "provider": p, "model": m, "stopReason": "end",
        "content": [{"type": "text", "text": '{"answer": "ok"}'}]}})
    return line + "\n"


@pytest.fixture
def env(tmp_path, monkeypatch):
    def setup(min_rank):
        monkeypatch.setattr(core, "PIPELINE_ROOT", tmp_path)
        monkeypatch.setattr(llm, "PROMPTS_DIR", tmp_path / "prompts")
        (tmp_path / "prompts").mkdir(exist_ok=True)
        (tmp_path / "prompts" / "t.md").write_text("no placeholders", encoding="utf-8")
        cfg = tmp_path / "config.yaml"
        cfg.write_text(
            "model_ranks:\n  m/primary: 2\n  m/lowrank: 1\n  m/fallback: 2\n"
            "stages:\n  t: {model: m/primary, min_rank: " + str(min_rank) +
            ", fallbacks: [m/lowrank, m/fallback]}\n", encoding="utf-8")
        monkeypatch.setattr(llm, "CONFIG_PATH", cfg)
        return core.connect(tmp_path / "t.db")
    yield setup


def _install(monkeypatch, script):
    calls = []

    def fake_run(cmd, **kw):
        model = cmd[cmd.index("--model") + 1]
        calls.append(model)
        behavior = script[len(calls) - 1]

        class Proc:
            stdout = _stream(behavior) if behavior != "crash" else ""
            stderr = "" if behavior != "crash" else "boom"
            returncode = 1 if behavior == "crash" else 0
        return Proc()

    monkeypatch.setattr(llm.subprocess, "run", fake_run)
    return calls


def test_crashing_primary_falls_back(env, monkeypatch):
    conn = env(min_rank=1)
    calls = _install(monkeypatch, ["crash", "m/lowrank"])
    res = llm.call("t", "t", {}, conn=conn)
    assert calls == ["m/primary", "m/lowrank"]
    assert res.ok and res.actual_model == "m/lowrank"
    row = conn.execute("SELECT requested_model, ok, fallback_errors FROM llm_calls").fetchone()
    assert row["requested_model"] == "m/primary" and row["ok"] == 1
    assert "m/primary" in row["fallback_errors"]  # the failed attempt is on the record


def test_below_min_rank_answer_falls_back(env, monkeypatch):
    conn = env(min_rank=2)
    calls = _install(monkeypatch, ["crash", "m/lowrank", "m/fallback"])
    res = llm.call("t", "t", {}, conn=conn)
    assert calls == ["m/primary", "m/lowrank", "m/fallback"]
    assert res.ok and res.actual_model == "m/fallback"  # the rank-1 answer could not pass


def test_chain_all_failing_records_failure(env, monkeypatch):
    conn = env(min_rank=2)
    _install(monkeypatch, ["crash", "m/lowrank", "crash"])
    res = llm.call("t", "t", {}, conn=conn)
    assert not res.ok and res.error == "omp exited 1: boom"  # the last attempt's honest error
    assert "m/lowrank" in res.fallback_errors[0]["error"] or \
           any("m/lowrank" in e["model"] for e in res.fallback_errors)  # earlier attempts on the record
    assert conn.execute("SELECT ok FROM llm_calls").fetchone()["ok"] == 0


def test_start_at_skips_the_models_that_already_failed_this_task(env, monkeypatch):
    conn = env(min_rank=2)
    calls = _install(monkeypatch, ["m/fallback"])
    res = llm.call("t", "t", {}, conn=conn, start_at=2)   # chain is [primary, lowrank, fallback]
    assert calls == ["m/fallback"] and res.ok
    calls2 = _install(monkeypatch, ["m/primary"])
    assert llm.call("t", "t", {}, conn=conn).ok and calls2 == ["m/primary"]   # default still starts at the primary
