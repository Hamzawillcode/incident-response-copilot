"""Human-readable printing of an investigation, shared by scripts and the watcher."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

RUNS_DIR = Path(__file__).resolve().parent.parent.parent / "runs"

_LABEL = {"tool_call": "ACT    ", "tool_result": "OBSERVE", "reasoning": "THINK  ", "thought": "THINK  ",
          "model_error": "ERROR  "}


def format_step(step: dict[str, Any]) -> str:
    kind = step.get("type")
    if kind == "tool_call":
        args = ", ".join(f"{k}={v!r}" for k, v in step.get("args", {}).items())
        return f"{_LABEL[kind]} {step['tool']}({args})"
    if kind == "tool_result":
        res = dict(step.get("result") or {})
        head = f"{_LABEL[kind]} {step['tool']} -> {res.pop('status', '?')} ({res.pop('size_bytes', 0) / 1024:.1f} KB)"
        lines = [head] + [f"          {k}: {_short(v)}" for k, v in res.items() if v not in (None, [], {})]
        return "\n".join(lines)
    if kind in ("reasoning", "thought"):
        return f"{_LABEL[kind]} {_short(step.get('text', ''), 400)}"
    if kind == "answer_submitted":
        return "SUBMIT  final answer"
    if kind == "model_error":
        return f"{_LABEL[kind]} {step.get('code')}: {step.get('message')}"
    if kind == "final":
        return "ANSWER\n" + format_diagnosis(step["diagnosis"])
    return json.dumps(step, default=str)[:300]


def format_diagnosis(d: dict[str, Any]) -> str:
    lines = [
        f"  incident:     {d.get('incident')}",
        f"  root cause:   {d.get('root_cause_service')}  ({d.get('failure_type')}, confidence {d.get('confidence')})",
        f"  summary:      {d.get('summary')}",
        f"  affected:     {', '.join(d.get('affected_services') or []) or '-'}",
        "  evidence:",
        *[f"    - [{e.get('kind')}] {e.get('detail')}" for e in d.get("evidence") or []],
    ]
    if d.get("alternative_causes"):
        lines.append(f"  alternatives: {', '.join(d['alternative_causes'])}")
    if d.get("suggested_next_checks"):
        lines.append("  next checks:")
        lines += [f"    - {c}" for c in d["suggested_next_checks"]]
    return "\n".join(lines)


def print_step(step: dict[str, Any]) -> None:
    print(format_step(step), flush=True)


def save_run(run_dict: dict[str, Any], prefix: str = "diagnosis") -> Path:
    """Write the full run (alert, steps, answer, tokens, latency) to runs/<timestamp>-<prefix>.json."""
    RUNS_DIR.mkdir(exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    path = RUNS_DIR / f"{stamp}-{prefix}.json"
    path.write_text(json.dumps(run_dict, indent=2, default=str))
    return path


def _short(value: Any, limit: int = 220) -> str:
    text = value if isinstance(value, str) else json.dumps(value, default=str)
    return text if len(text) <= limit else text[: limit - 3] + "..."
