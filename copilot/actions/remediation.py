"""Remediation actions for the practice environment (the OpenTelemetry Demo on this machine).

    rollback_config(service, reason)   restore the service's last known good configuration
    restart_service(service, reason)   docker restart <service>
    escalate_to_human(reason)          change nothing; hand over to a person

Safety
------
* ADK pauses every call to the two write tools until a human approves it
  (``require_confirmation=True`` in ``copilot.actions.ACTION_TOOLS``). As a second lock,
  each write tool itself refuses to run unless its ``tool_context`` carries a *confirmed*
  approval, so calling it directly from code cannot bypass the human either.
* Every call is appended to ``runs/actions/audit.jsonl`` (who approved, when, what, result).
  A rollback first saves a backup of the settings file in ``runs/actions/backups/``.
* ``COPILOT_ACTIONS_DRY_RUN=1`` reports what would change without changing anything.

What "configuration" means here
-------------------------------
In this practice environment, faults are injected through the demo's fault-injection
settings file (flagd). Rolling back a service's configuration therefore restores that
service's settings to their healthy values ("off"). This mapping is the practice
environment's stand-in for a real rollback (a Git revert, a config-store version, a
deployment rollback). It never leaves this module: results talk about the service, not
the settings, and pass through ``filters.scrub_output``. The diagnosing agents cannot see
any of it (DECISIONS.md D-005 and D-502).
"""

from __future__ import annotations

import json
import os
import shutil
import stat
import subprocess
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from copilot.tools import filters

__all__ = ["DEMO_SERVICES", "RESTARTABLE_SERVICES", "ROLLBACK_SERVICES", "escalate_to_human", "restart_service",
           "rollback_config"]

ROOT = Path(__file__).resolve().parents[2]
HEALTHY = "off"
MAX_REASON_CHARS = 500

#: The shop's own services (container name = service name in the demo's compose files).
DEMO_SERVICES = frozenset({
    "accounting", "ad", "cart", "checkout", "currency", "email", "fraud-detection", "frontend", "frontend-proxy",
    "image-provider", "kafka", "load-generator", "payment", "product-catalog", "quote", "recommendation",
    "shipping", "valkey-cart",
})
ROLLBACK_SERVICES = DEMO_SERVICES
#: Restart is limited to the shop. Never the fault-injection service, the databases or the observability stack.
RESTARTABLE_SERVICES = DEMO_SERVICES

# Practice environment only: the settings that make up each service's "configuration".
# A test checks every name against copilot.tools.filters.FLAG_KEYS and the demo's settings file.
_SETTINGS_BY_SERVICE: dict[str, tuple[str, ...]] = {
    "payment": ("paymentFailure", "paymentUnreachable"),
    "checkout": ("paymentUnreachable", "kafkaQueueProblems"),
    "product-catalog": ("productCatalogFailure", "productCatalogLockContention"),
    "recommendation": ("recommendationCacheFailure",),
    "ad": ("adFailure", "adHighCpu", "adManualGc"),
    "cart": ("cartFailure", "failedReadinessProbe"),
    "email": ("emailMemoryLeak",),
    "kafka": ("kafkaQueueProblems",),
    "fraud-detection": ("kafkaQueueProblems",),
    "accounting": ("kafkaQueueProblems",),
    "shipping": ("intlShippingSlowdown",),
    "frontend": ("imageSlowLoad",),
    "frontend-proxy": ("imageSlowLoad", "loadGeneratorFloodHomepage"),
    "image-provider": ("imageSlowLoad",),
    "load-generator": ("loadGeneratorFloodHomepage",),
}


# --------------------------------------------------------------------------- #
# The tools (their docstrings are what the Action agent reads)
# --------------------------------------------------------------------------- #
def rollback_config(service: str, reason: str, tool_context: Any = None) -> dict[str, Any]:
    """Roll back the most recent configuration change of a service to its last known good version.

    NEEDS HUMAN APPROVAL: the call pauses until the on-call engineer approves it.
    Use it when the diagnosis shows that a service started failing or slowing down
    because of a change to that service, for example rejected payment charges, a
    cache that grows without bound, or errors for one product. Roll back the
    root-cause service, not the services that only show symptoms.

    Args:
        service: The root-cause service, e.g. "payment".
        reason: One or two sentences: the root cause, the key evidence, and why a
            rollback fixes it.

    Returns:
        dict with ``status`` ``"ok"`` or ``"error"`` and a ``message``. ``changed``
        says whether anything had to be rolled back.
    """
    base = {"action": "rollback_config", "service": service}
    result = _guarded(base, service, reason, tool_context, ROLLBACK_SERVICES, _do_rollback)
    _audit("rollback_config", {"service": service, "reason": reason}, tool_context, result)
    return result


def restart_service(service: str, reason: str, tool_context: Any = None) -> dict[str, Any]:
    """Restart a service (its container).

    NEEDS HUMAN APPROVAL: the call pauses until the on-call engineer approves it.
    Use it to recover quickly when a service is stuck, unreachable or running out of
    memory. A restart does not remove the cause: a memory leak will come back, so
    prefer a rollback when a change caused the problem.

    Args:
        service: The service to restart, e.g. "recommendation".
        reason: One or two sentences: why a restart is the right step now.

    Returns:
        dict with ``status`` ``"ok"`` or ``"error"`` and a ``message``.
    """
    base = {"action": "restart_service", "service": service}
    result = _guarded(base, service, reason, tool_context, RESTARTABLE_SERVICES, _do_restart)
    _audit("restart_service", {"service": service, "reason": reason}, tool_context, result)
    return result


def escalate_to_human(reason: str) -> dict[str, Any]:
    """Hand the incident to the on-call engineer without changing anything.

    Use it when no safe automatic action fits: the evidence is weak, the cause is
    outside our services (a provider outage, real user traffic), or the fix needs a
    person (data repair, capacity planning).

    Args:
        reason: Why no automatic action is proposed, and what a human should look at first.

    Returns:
        dict with ``status`` ``"ok"`` and ``escalated`` = true.
    """
    if not isinstance(reason, str) or not reason.strip():
        return {"action": "escalate_to_human", "status": "error",
                "error": {"type": "invalid_input", "message": "reason must be a non-empty string."}}
    result = filters.scrub_output({"action": "escalate_to_human", "status": "ok", "escalated": True,
                                   "message": "Escalated to the on-call engineer. No automatic change was made.",
                                   "reason": reason.strip()[:MAX_REASON_CHARS]})
    _audit("escalate_to_human", {"reason": reason}, None, result)
    return result


# --------------------------------------------------------------------------- #
# Shared checks
# --------------------------------------------------------------------------- #
def _guarded(base: dict[str, Any], service: Any, reason: Any, tool_context: Any, allowed: frozenset[str],
             action) -> dict[str, Any]:
    try:
        problem = _validate(service, reason, allowed)
        if problem:
            return _error(base, "invalid_input", problem)
        if not _approved(tool_context):
            return _error(base, "not_approved", "This action needs a human approval and none was given.")
        return filters.scrub_output({**base, **action(service.strip().lower())})
    except Exception as exc:  # an action must report, never crash the agent loop
        return _error(base, "internal", f"Unexpected {type(exc).__name__} while running the action.")


def _validate(service: Any, reason: Any, allowed: frozenset[str]) -> str | None:
    if not isinstance(service, str) or service.strip().lower() not in allowed:
        return f"service must be one of: {', '.join(sorted(allowed))}."
    if not isinstance(reason, str) or not reason.strip():
        return "reason must be a non-empty string."
    if len(reason) > MAX_REASON_CHARS:
        return f"reason must be at most {MAX_REASON_CHARS} characters."
    return None


def _approved(tool_context: Any) -> bool:
    confirmation = getattr(tool_context, "tool_confirmation", None)
    return bool(getattr(confirmation, "confirmed", False))


def _error(base: dict[str, Any], type_: str, message: str) -> dict[str, Any]:
    return filters.scrub_output({**base, "status": "error", "error": {"type": type_, "message": message}})


def _dry_run() -> bool:
    return os.environ.get("COPILOT_ACTIONS_DRY_RUN", "").strip().lower() in ("1", "true", "yes")


def actions_dir() -> Path:
    return Path(os.environ.get("COPILOT_ACTIONS_DIR", "").strip() or ROOT / "runs" / "actions")


def settings_path() -> Path:
    raw = os.environ.get("FLAGD_CONFIG_PATH", "").strip()
    return Path(raw) if raw else ROOT / "opentelemetry-demo" / "src" / "flagd" / "demo.flagd.json"


# --------------------------------------------------------------------------- #
# Rollback (practice environment)
# --------------------------------------------------------------------------- #
def _do_rollback(service: str) -> dict[str, Any]:
    path = settings_path()
    if not path.is_file():
        return {"status": "error", "error": {"type": "configuration",
                                             "message": "The configuration store of the environment was not found."}}
    original = path.read_text(encoding="utf-8")
    data = json.loads(original)
    settings = data.get("flags", {})
    changed = 0
    for key in _SETTINGS_BY_SERVICE.get(service, ()):
        if key not in settings:
            continue
        healthy = healthy_setting(settings[key])
        if healthy != settings[key]:
            settings[key] = healthy
            changed += 1
    if not changed:
        return {"status": "ok", "changed": False,
                "message": f"{service} is already running its last known good configuration. Nothing was changed."}
    if _dry_run():
        return {"status": "ok", "changed": True, "dry_run": True,
                "message": f"Dry run: the configuration of {service} would be rolled back. Nothing was changed."}
    _backup(original, service)
    _atomic_write(path, json.dumps(data, indent=2) + "\n")
    return {"status": "ok", "changed": True,
            "message": f"The configuration of {service} was rolled back to its last known good version. "
                       "It takes effect within a few seconds; recovery shows in the metrics after a few minutes."}


def healthy_setting(setting: dict[str, Any]) -> dict[str, Any]:
    """The setting with every variant it can return replaced by the healthy one ("off").

    Targeting rules keep their structure (the demo's product rule stays a rule); only
    the variant names they return change.
    """
    variants = set((setting.get("variants") or {}).keys())
    if HEALTHY not in variants:
        raise ValueError("setting has no healthy variant")
    fixed = json.loads(json.dumps(setting))
    fixed["defaultVariant"] = HEALTHY
    if "targeting" in fixed:
        fixed["targeting"] = _replace_variants(fixed["targeting"], variants - {HEALTHY})
    return fixed


def _replace_variants(node: Any, names: set[str]) -> Any:
    if isinstance(node, str):
        return HEALTHY if node in names else node
    if isinstance(node, list):
        return [_replace_variants(item, names) for item in node]
    if isinstance(node, dict):  # {"var": "product_id"} names a variable, not a variant
        return {k: (v if k == "var" else _replace_variants(v, names)) for k, v in node.items()}
    return node


def _atomic_write(path: Path, text: str) -> None:
    """Write via a temporary file and rename, so readers never see half a file.

    The demo mounts the settings *directory* into its container, so the rename is visible
    there. The modification time must move forward: the settings service only reloads when it
    changes (DECISIONS.md D-207: a restore with the old mtime was silently ignored).
    """
    old = path.stat()
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".rollback-", suffix=".json")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(tmp, stat.S_IMODE(old.st_mode))
        os.replace(tmp, path)
    except BaseException:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise
    if path.stat().st_mtime_ns <= old.st_mtime_ns:
        bumped = old.st_mtime_ns + 1_000_000_000
        os.utime(path, ns=(bumped, bumped))


def _backup(original: str, service: str) -> Path:
    folder = actions_dir() / "backups"
    folder.mkdir(parents=True, exist_ok=True)
    target = folder / f"{_stamp()}-{service}-settings.json"
    target.write_text(original, encoding="utf-8")
    return target


# --------------------------------------------------------------------------- #
# Restart
# --------------------------------------------------------------------------- #
def _do_restart(service: str) -> dict[str, Any]:
    if _dry_run():
        return {"status": "ok", "dry_run": True, "message": f"Dry run: {service} would be restarted."}
    docker = shutil.which("docker")
    if docker is None:
        return {"status": "error", "error": {"type": "configuration", "message": "The docker CLI was not found."}}
    try:
        proc = run_command([docker, "restart", "-t", "10", service])
    except subprocess.TimeoutExpired:
        return {"status": "error", "error": {"type": "timeout", "message": f"Restarting {service} took too long."}}
    if proc.returncode != 0:
        detail = " ".join((proc.stderr or proc.stdout or "").split())[:200]
        return {"status": "error", "error": {"type": "restart_failed", "message": f"docker restart failed: {detail}"}}
    return {"status": "ok", "message": f"{service} was restarted."}


def run_command(args: list[str]) -> subprocess.CompletedProcess:
    """The only process this package starts (a list of arguments, never a shell). Tests replace it."""
    return subprocess.run(args, capture_output=True, text=True, timeout=120, check=False)


# --------------------------------------------------------------------------- #
# Audit log
# --------------------------------------------------------------------------- #
def _audit(tool: str, args: dict[str, Any], tool_context: Any, result: dict[str, Any]) -> None:
    confirmation = getattr(tool_context, "tool_confirmation", None)
    payload = getattr(confirmation, "payload", None)
    entry = {
        "at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "tool": tool,
        "args": args,
        "approved": bool(getattr(confirmation, "confirmed", False)),
        "approved_by": payload.get("by") if isinstance(payload, dict) else None,
        "incident_id": payload.get("incident_id") if isinstance(payload, dict) else None,
        "dry_run": _dry_run(),
        "status": result.get("status"),
        "changed": result.get("changed"),
        "message": (result.get("error") or {}).get("message") or result.get("message"),
    }
    try:
        folder = actions_dir()
        folder.mkdir(parents=True, exist_ok=True)
        with open(folder / "audit.jsonl", "a", encoding="utf-8") as handle:
            handle.write(json.dumps(entry, default=str) + "\n")
    except OSError:
        pass  # the audit log must never turn a successful action into a failure


def _stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
