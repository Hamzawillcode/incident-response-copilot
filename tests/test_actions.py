"""Unit tests for the remediation tools (copilot.actions): approval lock, rollback, restart, audit."""

from __future__ import annotations

import json
import os
import re
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from copilot.actions import ACTION_TOOLS, WRITE_TOOL_NAMES, escalate_to_human, remediation, restart_service, \
    rollback_config
from copilot.tools import filters

ROOT = Path(__file__).resolve().parent.parent
REAL_SETTINGS = ROOT / "opentelemetry-demo" / "src" / "flagd" / "demo.flagd.json"
LEAK = re.compile(r"flag|" + "|".join(filters.FLAG_KEYS), re.IGNORECASE)
APPROVED = SimpleNamespace(tool_confirmation=SimpleNamespace(confirmed=True,
                                                             payload={"by": "tester", "incident_id": "inc-1"}))
REJECTED = SimpleNamespace(tool_confirmation=SimpleNamespace(confirmed=False, payload=None))

SETTINGS = {"flags": {
    "paymentFailure": {"state": "ENABLED", "defaultVariant": "50%",
                       "variants": {"off": 0, "10%": 0.1, "50%": 0.5, "100%": 1}},
    "paymentUnreachable": {"state": "ENABLED", "defaultVariant": "off", "variants": {"on": True, "off": False}},
    "productCatalogFailure": {"state": "ENABLED", "defaultVariant": "off", "variants": {"on": True, "off": False},
                              "targeting": {"if": [{"==": [{"var": "product_id"}, "OLJCESPC7Z"]}, "on", "off"]}},
    "recommendationCacheFailure": {"state": "ENABLED", "defaultVariant": "on",
                                   "variants": {"on": True, "off": False}},
}}


@pytest.fixture
def settings(tmp_path, monkeypatch) -> Path:
    path = tmp_path / "flagd" / "demo.flagd.json"
    path.parent.mkdir()
    path.write_text(json.dumps(SETTINGS, indent=2))
    monkeypatch.setenv("FLAGD_CONFIG_PATH", str(path))
    return path


def flags(path: Path) -> dict:
    return json.loads(path.read_text())["flags"]


def audit(tmp_path: Path) -> list[dict]:
    log = tmp_path / "actions" / "audit.jsonl"
    return [json.loads(line) for line in log.read_text().splitlines()] if log.exists() else []


# --------------------------------------------------------------------------- #
# The approval lock
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("context", [None, REJECTED, SimpleNamespace(), SimpleNamespace(tool_confirmation=None)])
def test_write_tools_refuse_to_run_without_an_approval(settings, context):
    before = settings.read_text()
    for tool in (rollback_config, restart_service):
        out = tool("payment", "charges failing", context)
        assert out["status"] == "error" and out["error"]["type"] == "not_approved"
    assert settings.read_text() == before                      # nothing changed, no docker call (blocked)


# --------------------------------------------------------------------------- #
# Rollback
# --------------------------------------------------------------------------- #
def test_rollback_restores_only_the_services_settings(settings, tmp_path):
    out = rollback_config("payment", "payment charges failing since 11:53", APPROVED)
    assert out["status"] == "ok" and out["changed"] is True
    after = flags(settings)
    assert after["paymentFailure"]["defaultVariant"] == "off"
    assert after["recommendationCacheFailure"]["defaultVariant"] == "on"     # another service: untouched
    assert after["productCatalogFailure"] == SETTINGS["flags"]["productCatalogFailure"]
    backups = list((tmp_path / "actions" / "backups").glob("*-payment-settings.json"))
    assert len(backups) == 1 and json.loads(backups[0].read_text()) == SETTINGS
    entry = audit(tmp_path)[-1]
    assert entry["tool"] == "rollback_config" and entry["approved_by"] == "tester" and entry["changed"] is True


def test_rollback_results_never_name_the_settings(settings):
    for service in ("payment", "product-catalog", "recommendation", "checkout", "currency"):
        text = json.dumps(rollback_config(service, "why", APPROVED))
        assert not LEAK.search(text), text


def test_second_rollback_changes_nothing(settings):
    rollback_config("payment", "why", APPROVED)
    out = rollback_config("payment", "why", APPROVED)
    assert out["status"] == "ok" and out["changed"] is False and "Nothing was changed" in out["message"]


def test_service_without_recorded_changes_is_a_no_op(settings):
    before = settings.read_text()
    out = rollback_config("currency", "why", APPROVED)
    assert out["changed"] is False and settings.read_text() == before


def test_targeting_rules_keep_their_structure(settings):
    rollback_config("product-catalog", "one product fails", APPROVED)
    targeting = flags(settings)["productCatalogFailure"]["targeting"]
    assert targeting == {"if": [{"==": [{"var": "product_id"}, "OLJCESPC7Z"]}, "off", "off"]}


def test_the_modification_time_always_moves_forward(settings):
    future = settings.stat().st_mtime_ns + 3_600_000_000_000            # an hour ahead (clock skew)
    os.utime(settings, ns=(future, future))
    rollback_config("payment", "why", APPROVED)
    assert settings.stat().st_mtime_ns > future                        # the settings service reloads


def test_dry_run_changes_nothing(settings, monkeypatch):
    monkeypatch.setenv("COPILOT_ACTIONS_DRY_RUN", "1")
    before = settings.read_text()
    out = rollback_config("payment", "why", APPROVED)
    assert out["dry_run"] is True and out["changed"] is True and settings.read_text() == before


def test_missing_settings_file_is_a_configuration_error():
    out = rollback_config("payment", "why", APPROVED)                  # conftest points it at a missing file
    assert out["status"] == "error" and out["error"]["type"] == "configuration"


@pytest.mark.parametrize("service", ["flagd", "flagd-ui", "otel-collector", "astronomy-db", "", None, 7,
                                     "payment; rm -rf /"])
def test_only_shop_services_can_be_rolled_back(settings, service):
    out = rollback_config(service, "why", APPROVED)
    assert out["error"]["type"] == "invalid_input"


@pytest.mark.parametrize("reason", ["", "  ", None, "x" * 501])
def test_a_reason_is_required(settings, reason):
    assert rollback_config("payment", reason, APPROVED)["error"]["type"] == "invalid_input"


def test_service_names_are_case_insensitive(settings):
    assert rollback_config("Payment", "why", APPROVED)["changed"] is True


def test_healthy_setting_needs_a_healthy_variant():
    with pytest.raises(ValueError):
        remediation.healthy_setting({"defaultVariant": "on", "variants": {"on": True}})


# --------------------------------------------------------------------------- #
# Restart
# --------------------------------------------------------------------------- #
@pytest.fixture
def docker(monkeypatch):
    calls: list[list[str]] = []
    result = {"returncode": 0, "stderr": ""}

    def fake_run(args):
        calls.append(args)
        return subprocess.CompletedProcess(args, result["returncode"], stdout="", stderr=result["stderr"])

    monkeypatch.setattr(remediation.shutil, "which", lambda name: "/usr/local/bin/docker")
    monkeypatch.setattr(remediation, "run_command", fake_run)
    return SimpleNamespace(calls=calls, result=result)


def test_restart_runs_docker_restart_without_a_shell(docker):
    out = restart_service("recommendation", "memory keeps growing", APPROVED)
    assert out["status"] == "ok" and docker.calls == [["/usr/local/bin/docker", "restart", "-t", "10",
                                                       "recommendation"]]


def test_restart_failure_is_reported(docker):
    docker.result.update(returncode=1, stderr="Error: No such container: recommendation")
    out = restart_service("recommendation", "why", APPROVED)
    assert out["error"]["type"] == "restart_failed" and "No such container" in out["error"]["message"]


@pytest.mark.parametrize("service", ["flagd", "prometheus", "jaeger", "opensearch", "grafana", "astronomy-db"])
def test_restart_is_limited_to_the_shop(docker, service):
    assert restart_service(service, "why", APPROVED)["error"]["type"] == "invalid_input"
    assert docker.calls == []


def test_restart_dry_run_and_missing_docker(docker, monkeypatch):
    monkeypatch.setenv("COPILOT_ACTIONS_DRY_RUN", "true")
    assert restart_service("cart", "why", APPROVED)["dry_run"] is True and docker.calls == []
    monkeypatch.delenv("COPILOT_ACTIONS_DRY_RUN")
    monkeypatch.setattr(remediation.shutil, "which", lambda name: None)
    assert restart_service("cart", "why", APPROVED)["error"]["type"] == "configuration"


# --------------------------------------------------------------------------- #
# Escalation and the ADK wrappers
# --------------------------------------------------------------------------- #
def test_escalation_changes_nothing_and_needs_no_approval(tmp_path):
    out = escalate_to_human("evidence is weak: only one log pattern")
    assert out["status"] == "ok" and out["escalated"] is True
    assert escalate_to_human("")["status"] == "error"
    assert audit(tmp_path)[-1]["tool"] == "escalate_to_human"


def test_adk_tools_require_confirmation_exactly_for_the_write_tools():
    by_name = {t.name: t for t in ACTION_TOOLS}
    assert set(by_name) == WRITE_TOOL_NAMES | {"escalate_to_human"}
    for name, tool in by_name.items():
        assert bool(tool._require_confirmation) is (name in WRITE_TOOL_NAMES)


def test_the_model_never_sees_tool_context_and_docstrings_never_name_the_mechanism():
    for tool in ACTION_TOOLS:
        declaration = tool._get_declaration()
        schema = declaration.parameters_json_schema or declaration.parameters.model_dump(exclude_none=True)
        assert "tool_context" not in json.dumps(schema)
        assert not LEAK.search(declaration.description or ""), tool.name


def test_every_mapped_setting_exists():
    keys = {k for keys in remediation._SETTINGS_BY_SERVICE.values() for k in keys}
    assert keys <= set(filters.FLAG_KEYS)
    assert set(remediation._SETTINGS_BY_SERVICE) <= remediation.ROLLBACK_SERVICES
    if REAL_SETTINGS.exists():
        real = json.loads(REAL_SETTINGS.read_text())["flags"]
        for key in keys:
            assert key in real and "off" in real[key]["variants"], key
