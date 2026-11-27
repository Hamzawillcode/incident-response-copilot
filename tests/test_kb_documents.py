"""Unit tests for parsing and chunking knowledge documents, and for the real kb/ content."""

from __future__ import annotations

import re
import typing
from pathlib import Path

import pytest

from copilot.agents.diagnosis import FailureType
from copilot.kb.documents import (
    FAILURE_TYPES, MAX_CHUNK_CHARS, DocumentError, load_chunks, load_documents, parse_document, split_long,
    split_sections,
)
from copilot.tools import filters

ROOT = Path(__file__).resolve().parent.parent
KB_DIR = ROOT / "kb"
LEAK = re.compile(r"flag|ofrep|openfeature|" + "|".join(filters.FLAG_KEYS), re.IGNORECASE)

RUNBOOK = """---
services: [payment]
tags: [charge, card]
last_reviewed: 2026-10-15
---
# Payment service: charge failures

Intro before the first section.

## Symptoms
- payment errors

## Diagnosis
### Step one
Check the traces.
"""

POSTMORTEM = """---
incident_id: INC-1
date: 2026-03-14
severity: SEV-2
services: [payment, checkout]
root_cause_service: payment
failure_type: errors
duration_minutes: 47
---
# Postmortem: payments rejected

## Summary
Charges failed.
"""


def parse(text: str = RUNBOOK, kind: str = "runbook", source: str = "kb/runbooks/payment.md"):
    return parse_document(text, kind=kind, source=source)


# --------------------------------------------------------------------------- #
# Parsing
# --------------------------------------------------------------------------- #
def test_parse_reads_front_matter_title_and_body():
    doc = parse()
    assert doc.kind == "runbook" and doc.doc_id == "payment" and doc.source == "kb/runbooks/payment.md"
    assert doc.title == "Payment service: charge failures"
    assert doc.metadata == {"services": ["payment"], "tags": ["charge", "card"], "last_reviewed": "2026-10-15"}


def test_chunks_are_sections_with_an_overview_for_the_intro():
    chunks = parse().chunks()
    assert [c.heading for c in chunks] == ["Overview", "Symptoms", "Diagnosis"]
    assert chunks[0].text == "Intro before the first section."
    assert "### Step one" in chunks[2].text                        # level-3 headings stay inside the section
    assert [c.position for c in chunks] == [0, 1, 2]
    assert [c.id for c in chunks] == ["runbook/payment#00-overview", "runbook/payment#01-symptoms",
                                      "runbook/payment#02-diagnosis"]
    assert all(c.keywords == "payment charge card" for c in chunks)


def test_embedding_text_gives_each_chunk_its_context():
    chunk = parse().chunks()[1]
    assert chunk.embedding_text == "Payment service: charge failures\nSymptoms\n\n- payment errors"


def test_content_hash_is_stable_and_changes_with_the_text_only_in_that_section():
    before = {c.id: c.content_hash for c in parse().chunks()}
    assert before == {c.id: c.content_hash for c in parse().chunks()}
    after = {c.id: c.content_hash for c in parse(RUNBOOK.replace("payment errors", "payment errors rising")).chunks()}
    changed = [cid for cid in before if before[cid] != after[cid]]
    assert changed == ["runbook/payment#01-symptoms"]


def test_headings_inside_code_blocks_do_not_start_sections():
    text = RUNBOOK + "\n## Commands\n```bash\n## not a heading\n# not a title either\n```\nDone.\n"
    chunks = parse(text).chunks()
    assert [c.heading for c in chunks][-1] == "Commands"
    assert "## not a heading" in chunks[-1].text


def test_postmortem_metadata_is_validated_and_dates_become_strings():
    doc = parse(POSTMORTEM, kind="postmortem", source="kb/postmortems/2026-03-14-payments.md")
    assert doc.metadata["date"] == "2026-03-14" and doc.metadata["failure_type"] == "errors"
    assert doc.chunks()[0].id == "postmortem/2026-03-14-payments#00-summary"


def test_crlf_line_endings_are_accepted():
    assert parse(RUNBOOK.replace("\n", "\r\n")).title == "Payment service: charge failures"


# --------------------------------------------------------------------------- #
# Long sections
# --------------------------------------------------------------------------- #
def test_long_sections_are_split_at_blank_lines_and_keep_code_blocks_whole():
    code = "```\nline one\n\nline three\n```"
    paragraphs = [f"Paragraph {i} " + "x" * 300 for i in range(8)]
    text = "\n\n".join(paragraphs[:4] + [code] + paragraphs[4:])
    parts = split_long(text, 800)
    assert len(parts) > 1 and all(len(p) <= 800 for p in parts)
    assert any(code in p for p in parts)                             # the fenced block was not cut in half
    assert "\n\n".join(parts) == text                                # nothing lost or reordered


def test_a_single_huge_paragraph_is_hard_split():
    parts = split_long("y" * 2500, 1000)
    assert [len(p) for p in parts] == [1000, 1000, 500]


def test_long_section_parts_get_numbered_headings():
    body = "\n\n".join("z" * 400 for _ in range(6))
    doc = parse(RUNBOOK + f"\n## Long\n{body}\n")
    headings = [c.heading for c in doc.chunks(max_chars=1000)]
    assert "Long (part 1)" in headings and "Long (part 2)" in headings


def test_split_sections_skips_empty_sections():
    assert split_sections("## A\n\n## B\ntext\n") == [("B", "text")]


# --------------------------------------------------------------------------- #
# Invalid documents
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("text, problem", [
    ("# Title\n## A\ntext\n", "front matter"),
    ("---\n- a\n- b\n---\n# T\n## S\nx\n", "mapping"),
    ("---\nservices: [a\n---\n# T\n## S\nx\n", "invalid YAML"),
    ("---\ntags: [a]\n---\n# T\n## S\nx\n", "services"),
    ("---\nservices: []\n---\n# T\n## S\nx\n", "services"),
    ("---\nservices: [a]\ntags: a\n---\n# T\n## S\nx\n", "tags"),
    ("---\nservices: [a]\n---\n## Not a title\nx\n", "# Title"),
    ("---\nservices: [a]\n---\n# One\n## S\nx\n# Two\n", "only one"),
    ("---\nservices: [a]\n---\n# Title only\n", "no content"),
])
def test_malformed_runbooks_are_rejected_with_the_file_name(text, problem):
    with pytest.raises(DocumentError) as info:
        parse(text)
    assert "kb/runbooks/payment.md" in str(info.value) and problem in str(info.value)


@pytest.mark.parametrize("change", [
    ("incident_id: INC-1\n", ""),
    ("failure_type: errors", "failure_type: cosmic"),
    ("duration_minutes: 47", "duration_minutes: -1"),
    ("duration_minutes: 47", "duration_minutes: true"),
    ("root_cause_service: payment", "root_cause_service: ''"),
])
def test_postmortems_need_complete_metadata(change):
    with pytest.raises(DocumentError):
        parse(POSTMORTEM.replace(*change), kind="postmortem", source="kb/postmortems/x.md")


@pytest.mark.parametrize("source", ["kb/runbooks/Payment.md", "kb/runbooks/pay ment.md", "kb/runbooks/-x.md"])
def test_file_names_must_be_simple(source):
    with pytest.raises(DocumentError, match="file name"):
        parse(source=source)


@pytest.mark.parametrize("term", ["feature flag", "Feature-Flags", "flagd", "paymentFailure", "OpenFeature",
                                  "OFREP", "flagservice", "kafkaQueueProblems"])
def test_fault_injection_terms_are_refused_with_the_line_number(term):
    text = RUNBOOK.replace("Check the traces.", f"Check the traces and {term}.")
    with pytest.raises(DocumentError) as info:
        parse(text)
    assert "kb/runbooks/payment.md:15" in str(info.value) and "fault-injection" in str(info.value)


def test_fault_injection_terms_in_front_matter_are_refused_too():
    with pytest.raises(DocumentError):
        parse(RUNBOOK.replace("tags: [charge, card]", "tags: [charge, flagd]"))


def test_unknown_kind_is_rejected():
    with pytest.raises(DocumentError):
        parse(kind="howto")


def test_missing_directory_is_reported(tmp_path):
    with pytest.raises(DocumentError, match="not found"):
        load_documents(tmp_path / "nope")


def test_load_documents_reads_both_folders(tmp_path):
    (tmp_path / "runbooks").mkdir()
    (tmp_path / "postmortems").mkdir()
    (tmp_path / "runbooks" / "payment.md").write_text(RUNBOOK)
    (tmp_path / "postmortems" / "inc-1.md").write_text(POSTMORTEM)
    (tmp_path / "runbooks" / "notes.txt").write_text("ignored")
    docs = load_documents(tmp_path)
    assert [(d.kind, d.source) for d in docs] == [("runbook", f"{tmp_path.name}/runbooks/payment.md"),
                                                  ("postmortem", f"{tmp_path.name}/postmortems/inc-1.md")]


# --------------------------------------------------------------------------- #
# The real knowledge base in kb/
# --------------------------------------------------------------------------- #
KNOWN_SERVICES = {
    "frontend", "frontend-proxy", "checkout", "cart", "payment", "product-catalog", "currency", "shipping",
    "quote", "email", "recommendation", "ad", "accounting", "fraud-detection", "image-provider", "kafka",
    "load-generator", "valkey-cart", "astronomy-db",
}


def test_real_knowledge_base_has_the_planned_size():
    docs = load_documents(KB_DIR)
    assert 10 <= sum(d.kind == "runbook" for d in docs) <= 15        # guide: 10-15 runbooks
    assert sum(d.kind == "postmortem" for d in docs) == 5            # guide: 5 seed postmortems


def test_every_runbook_has_symptoms_diagnosis_and_remediation():
    for doc in load_documents(KB_DIR):
        headings = {c.heading for c in doc.chunks()}
        if doc.kind == "runbook" and doc.doc_id not in ("triage-symptom-vs-cause", "known-noise-and-false-alarms"):
            assert {"Symptoms", "Diagnosis", "Remediation"} <= headings, doc.source
        if doc.kind == "postmortem":
            assert {"Summary", "Root cause", "Resolution"} <= headings, doc.source


def test_metadata_names_only_real_services_and_failure_types():
    for doc in load_documents(KB_DIR):
        assert set(doc.metadata["services"]) <= KNOWN_SERVICES, doc.source
        if doc.kind == "postmortem":
            assert doc.metadata["root_cause_service"] in KNOWN_SERVICES | {"none"}, doc.source


def test_no_knowledge_document_mentions_fault_injection():
    """Broader than the ingestion check: even the word 'flag' is absent from every document."""
    files = sorted(KB_DIR.glob("*/*.md"))
    assert len(files) >= 15
    for path in files:
        match = LEAK.search(path.read_text())
        assert match is None, f"{path.name}: {match.group(0)!r}"


def test_all_real_chunks_fit_the_size_limit_and_have_unique_ids():
    chunks = load_chunks(KB_DIR)
    assert len(chunks) == len({c.id for c in chunks})
    assert all(0 < len(c.text) <= MAX_CHUNK_CHARS for c in chunks)


def test_failure_types_match_the_diagnosis_schema():
    assert FAILURE_TYPES == set(typing.get_args(FailureType))
