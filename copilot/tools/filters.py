"""Telemetry sanitization: keep feature-flag (fault-injection) state away from callers.

The copilot's diagnosis agents must find the cause of an injected fault from
symptoms alone. In OpenTelemetry Demo 3.1.0 the flag state leaks through every
signal (all observed in the running demo, see DECISIONS.md D-005/D-107/D-203):

* traces  - spans from ``flagd``/``flagd-ui``; client spans calling flagd
            (``rpc.service=flagd.evaluation.v2.Service``, ``server.address=flagd``,
            OFREP ``/ofrep/v1/evaluate``); ``feature_flag.evaluation`` span events
            carrying key/variant/value; ``feature_flag.*`` and
            ``demo.feature_flag.*`` attributes; status messages and event names
            such as "Error: Product Catalog Fail Feature Flag Enabled".
* logs    - Envoy access logs of ``POST /flagservice/...``; shipping's
            ``shipping.feature_flag.evaluated`` events with ``feature_flag_key``
            and ``feature_flag_variant``; fault narration such as
            "Feature Flag adManualGc enabled, performing a manual gc now".
* metrics - ``feature_flag_evaluation_*`` series with ``feature_flag_key`` /
            ``feature_flag_result_variant`` labels; ``job``/``service_name``/
            ``span_name``/``container_name`` values naming flagd.

Policy
------
* Records produced by, or talking to, the flag service are dropped.
* Spans represent real work, so they are kept but stripped: flag attributes and
  flag events are removed and any free text that mentions flags is replaced
  *as a whole* by ``[redacted]`` (partial redaction such as "shipment delayed
  due to [redacted]" would still reveal the cause).
* Log records that mention flags anywhere are narration of the injection and
  are dropped entirely.
* Caller-supplied inputs that mention flags are refused, so result counts
  cannot be used as an oracle.
* ``scrub_output`` is a final, structure-agnostic safety net that every tool
  applies to its result just before returning it.

This module only transforms Python data; it performs no I/O.
"""

from __future__ import annotations

import re
from typing import Any

REDACTED = "[redacted]"
REFUSAL_MESSAGE = "This request targets restricted telemetry and is not permitted."

RESTRICTED_SERVICES = frozenset({"flagd", "flagd-ui"})

# Flag keys defined in opentelemetry-demo/src/flagd/demo.flagd.json (3.1.0).
# A test asserts this list stays in sync with that file.
FLAG_KEYS = (
    "adFailure", "adHighCpu", "adManualGc", "aiRunawayAgent", "aiSlowResponse",
    "cartFailure", "emailMemoryLeak", "emitRawPii", "failedReadinessProbe",
    "imageSlowLoad", "intlShippingSlowdown", "kafkaQueueProblems",
    "loadGeneratorFloodHomepage", "paymentFailure", "paymentUnreachable",
    "productCatalogFailure", "productCatalogLockContention", "recommendationCacheFailure",
)

# Traffic to/from the flag service (endpoints, RPC services, protocols).
_FLAG_SERVICE = re.compile(r"flagd|flagservice|ofrep|openfeature", re.IGNORECASE)
# Attribute / label / event *names*: feature_flag.*, demo.feature_flag.*,
# shipping.feature_flag.*, feature_flag_key, FeatureFlag..., flagd...
_FLAG_NAME = re.compile(r"feature[\s_.\-]*flag|flagd|openfeature", re.IGNORECASE)
# Any free text that reveals flag state or the injection mechanism.
_FLAG_TEXT = re.compile(
    r"feature[\s_.\-]*flags?|flagd|flagservice|ofrep|openfeature|\b(?:"
    + "|".join(FLAG_KEYS)
    + r")\b",
    re.IGNORECASE,
)
_LABEL_SET = re.compile(r"\{[^{}]*\}")


# --------------------------------------------------------------------------- #
# Predicates
# --------------------------------------------------------------------------- #
def mentions_flags(value: Any) -> bool:
    """True if a string (or str() of a scalar) reveals flag information."""
    return isinstance(value, str) and bool(_FLAG_TEXT.search(value))


def is_flag_name(name: Any) -> bool:
    """True for attribute/label/event names such as ``feature_flag.key``."""
    return isinstance(name, str) and bool(_FLAG_NAME.search(name))


def is_restricted_service(name: Any) -> bool:
    return isinstance(name, str) and (name.strip().lower() in RESTRICTED_SERVICES or name.lower().startswith("flagd"))


def _touches_flag_service(value: Any) -> bool:
    if isinstance(value, str):
        return bool(_FLAG_SERVICE.search(value))
    if isinstance(value, (list, tuple)):
        return any(_touches_flag_service(v) for v in value)
    return False


# --------------------------------------------------------------------------- #
# Inputs
# --------------------------------------------------------------------------- #
def refuse_inputs(*values: Any) -> str | None:
    """Return a refusal message if any caller-supplied value targets flag data."""
    for value in values:
        if value is None:
            continue
        if is_restricted_service(value) or mentions_flags(value):
            return REFUSAL_MESSAGE
    return None


# --------------------------------------------------------------------------- #
# Text and attributes
# --------------------------------------------------------------------------- #
def redact_text(text: str) -> str:
    """Redact flag information from free text.

    Label sets like ``{job="opentelemetry-demo/flagd-ui", ...}`` (as quoted in
    Prometheus error messages) are replaced individually so the rest of the
    message stays useful; if anything still mentions flags afterwards, the
    whole string is replaced.
    """
    if not isinstance(text, str) or not mentions_flags(text):
        return text
    text = _LABEL_SET.sub(lambda m: "{" + REDACTED + "}" if mentions_flags(m.group(0)) else m.group(0), text)
    return REDACTED if mentions_flags(text) else text


def _redact_value(value: Any) -> Any:
    if isinstance(value, str):
        return redact_text(value)
    if isinstance(value, list):
        return [_redact_value(v) for v in value]
    if isinstance(value, dict):
        return sanitize_attributes(value)
    return value


def sanitize_attributes(attributes: dict[str, Any] | None) -> dict[str, Any]:
    """Drop flag-named attributes (at any depth) and redact flag-revealing values."""
    return {k: _redact_value(v) for k, v in (attributes or {}).items() if not is_flag_name(k)}


def labels_are_restricted(labels: dict[str, Any]) -> bool:
    """For metric series: any flag-named label or flag-revealing label value."""
    return any(is_flag_name(k) or mentions_flags(str(v)) for k, v in labels.items())


# --------------------------------------------------------------------------- #
# Spans and logs (normalized, backend-independent shapes)
# --------------------------------------------------------------------------- #
def sanitize_span(span: dict[str, Any]) -> dict[str, Any] | None:
    """Sanitize a normalized span; return ``None`` if it must be dropped.

    Expected keys: ``service``, ``operation``, ``attributes`` (dict),
    ``events`` (list of ``{"name", "attributes", ...}``), ``status_message``.
    Other keys are passed through unchanged.
    """
    attributes = span.get("attributes") or {}
    if (
        is_restricted_service(span.get("service"))
        or _touches_flag_service(span.get("operation"))
        or mentions_flags(span.get("operation"))
        or any(_touches_flag_service(v) for v in attributes.values())
    ):
        return None

    clean = dict(span)
    clean["attributes"] = sanitize_attributes(attributes)
    clean["events"] = [
        {**event, "attributes": sanitize_attributes(event.get("attributes"))}
        for event in span.get("events") or []
        if not _event_is_flag_related(event)
    ]
    if span.get("status_message") is not None:
        clean["status_message"] = redact_text(span["status_message"])
    return clean


def _event_is_flag_related(event: dict[str, Any]) -> bool:
    name = event.get("name")
    attributes = event.get("attributes") or {}
    return (
        mentions_flags(name)
        or is_flag_name(name)
        or any(is_flag_name(k) for k in attributes)
        or any(_touches_flag_service(v) for v in attributes.values())
    )


def sanitize_log(record: dict[str, Any]) -> dict[str, Any] | None:
    """Sanitize a normalized log record; return ``None`` if it must be dropped.

    Expected keys: ``service``, ``body``, ``event_name``, ``attributes``.

    * Dropped: records from the flag service; records whose message or event
      name mentions flags (fault narration such as "Feature Flag adManualGc
      enabled ..."); records with flag-named attributes (shipping's
      ``feature_flag_key``/``feature_flag_variant`` events); records whose
      attribute values point at the flag service (Envoy ``/flagservice/`` logs).
    * Kept but redacted: organic errors that merely *carry* flag text in an
      attribute value, e.g. frontend's "API request failed" with
      ``exception.message`` = "13 INTERNAL: Error: Product Catalog Fail Feature
      Flag Enabled". The error is real evidence; only the value is replaced.
      (Keeping these also keeps record counts consistent with the backend's.)
    """
    attributes = record.get("attributes") or {}
    if (
        is_restricted_service(record.get("service"))
        or mentions_flags(record.get("body"))
        or mentions_flags(record.get("event_name"))
        or is_flag_name(record.get("event_name"))
        or any(is_flag_name(k) for k in _flatten_keys(attributes))
        or any(_touches_flag_service(v) for v in _flatten_strings(attributes))
    ):
        return None
    clean = dict(record)
    clean["attributes"] = sanitize_attributes(attributes)
    return clean


def _flatten_keys(value: Any):
    if isinstance(value, dict):
        for k, v in value.items():
            yield k
            yield from _flatten_keys(v)


def _flatten_strings(value: Any):
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for v in value.values():
            yield from _flatten_strings(v)
    elif isinstance(value, (list, tuple)):
        for v in value:
            yield from _flatten_strings(v)


# --------------------------------------------------------------------------- #
# Final safety net
# --------------------------------------------------------------------------- #
def scrub_output(value: Any) -> Any:
    """Recursively remove flag-named keys and redact flag-revealing strings.

    Applied by every tool to its complete result right before returning it, so
    that a bug in record-level sanitization cannot leak flag state.
    """
    if isinstance(value, dict):
        return {k: scrub_output(v) for k, v in value.items() if not (isinstance(k, str) and is_flag_name(k))}
    if isinstance(value, list):
        return [scrub_output(v) for v in value]
    if isinstance(value, tuple):
        return tuple(scrub_output(v) for v in value)
    if isinstance(value, str):
        return redact_text(value)
    return value
