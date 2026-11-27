"""Knowledge documents: parse the Markdown runbooks and postmortems and split them into chunks.

A document is one Markdown file in ``kb/runbooks`` or ``kb/postmortems``::

    ---
    services: [payment]                  <- YAML front matter (metadata)
    tags: [charge, card]
    ---
    # Payment service: charge failures   <- exactly one title
    ## Overview                           <- every "##" section becomes one chunk
    ...

Why one chunk per section: a runbook section ("Symptoms", "Diagnosis", "Remediation")
is the unit an engineer, or an agent, actually needs. Whole files would dilute the
match and cost tokens; fixed-size windows would cut steps in half. Sections longer
than ``MAX_CHUNK_CHARS`` are split at blank lines, never inside a code block. Text
between the title and the first "##" becomes an "Overview" chunk.

Every chunk keeps its document title and heading (``embedding_text``), so it still
makes sense when it is embedded or shown on its own.

Documents that mention how faults are injected in the test environment are refused
(same patterns as the telemetry filters): the knowledge base must never hand the
diagnosis agents the answer.

Only :func:`load_documents` reads files; everything else is pure.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path
from typing import Any

import yaml

from copilot.tools import filters

__all__ = ["Chunk", "Document", "DocumentError", "KINDS", "chunk_document", "load_chunks", "load_documents",
           "parse_document"]

#: kind -> folder inside the knowledge-base directory.
KINDS = {"runbook": "runbooks", "postmortem": "postmortems"}
MAX_CHUNK_CHARS = 1500
OVERVIEW = "Overview"
# Must match copilot.agents.diagnosis.FailureType (a test checks this).
FAILURE_TYPES = frozenset({"errors", "latency", "resource", "queue_lag", "traffic_spike", "unavailable", "none",
                           "unknown"})

_DOC_ID = re.compile(r"[a-z0-9][a-z0-9-]{0,99}")
_FRONT_MATTER = re.compile(r"\A---[ \t]*\n(.*?)\n---[ \t]*\n", re.DOTALL)
_FENCE = re.compile(r"^\s*(```|~~~)")
_H1 = re.compile(r"^#\s+(.+?)\s*#*\s*$")
_H2 = re.compile(r"^##\s+(.+?)\s*#*\s*$")


class DocumentError(ValueError):
    """A knowledge document is malformed or not allowed. The message names the file."""


@dataclass(frozen=True)
class Chunk:
    """One searchable section of a document."""

    id: str                 # "runbook/payment#03-diagnosis"
    kind: str               # "runbook" | "postmortem"
    doc_id: str             # "payment"
    source: str             # "kb/runbooks/payment.md"
    title: str              # document title
    heading: str            # section heading
    position: int           # 0-based order of the section in the document
    text: str               # section body (Markdown)
    keywords: str           # services and tags from the front matter, space separated
    metadata: dict[str, Any] = field(default_factory=dict, compare=False, hash=False)

    @property
    def embedding_text(self) -> str:
        """What gets embedded: title and heading give the section its context."""
        return f"{self.title}\n{self.heading}\n\n{self.text}"

    @property
    def content_hash(self) -> str:
        """Changes whenever anything stored for this chunk changes (drives re-embedding)."""
        payload = json.dumps([self.kind, self.source, self.title, self.heading, self.position, self.text,
                              self.keywords, self.metadata], sort_keys=True, default=str)
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class Document:
    kind: str
    doc_id: str
    source: str
    title: str
    body: str
    metadata: dict[str, Any] = field(default_factory=dict, compare=False, hash=False)

    def chunks(self, max_chars: int = MAX_CHUNK_CHARS) -> list[Chunk]:
        return chunk_document(self, max_chars=max_chars)


# --------------------------------------------------------------------------- #
# Loading
# --------------------------------------------------------------------------- #
def load_documents(kb_dir: str | Path) -> list[Document]:
    """Read and validate every ``*.md`` file in ``<kb_dir>/runbooks`` and ``<kb_dir>/postmortems``."""
    kb_dir = Path(kb_dir)
    if not kb_dir.is_dir():
        raise DocumentError(f"{kb_dir}: knowledge-base directory not found.")
    docs: list[Document] = []
    for kind, folder in KINDS.items():
        for path in sorted((kb_dir / folder).glob("*.md")):
            source = f"{kb_dir.name}/{folder}/{path.name}"
            try:
                text = path.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError) as exc:
                raise DocumentError(f"{source}: cannot be read ({type(exc).__name__}).") from None
            docs.append(parse_document(text, kind=kind, source=source))
    return docs


def load_chunks(kb_dir: str | Path, max_chars: int = MAX_CHUNK_CHARS) -> list[Chunk]:
    chunks = [chunk for doc in load_documents(kb_dir) for chunk in doc.chunks(max_chars)]
    seen: set[str] = set()
    for chunk in chunks:
        if chunk.id in seen:  # cannot happen with unique file names; guards future changes
            raise DocumentError(f"{chunk.source}: duplicate chunk id {chunk.id!r}.")
        seen.add(chunk.id)
    return chunks


# --------------------------------------------------------------------------- #
# Parsing
# --------------------------------------------------------------------------- #
def parse_document(text: str, *, kind: str, source: str) -> Document:
    """Parse one Markdown file. Raises :class:`DocumentError` with the file name on any problem."""
    if kind not in KINDS:
        raise DocumentError(f"{source}: unknown document kind {kind!r}.")
    doc_id = Path(source).stem
    if not _DOC_ID.fullmatch(doc_id):
        raise DocumentError(f"{source}: file name must be lowercase letters, digits and dashes.")
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    _refuse_fault_injection_terms(text, source)

    match = _FRONT_MATTER.match(text)
    if not match:
        raise DocumentError(f"{source}: must start with YAML front matter between '---' lines.")
    try:
        raw_meta = yaml.safe_load(match.group(1))
    except yaml.YAMLError as exc:
        raise DocumentError(f"{source}: invalid YAML front matter ({type(exc).__name__}).") from None
    if not isinstance(raw_meta, dict):
        raise DocumentError(f"{source}: front matter must be a mapping of keys to values.")
    metadata = _validate_metadata(_jsonable(raw_meta), kind, source)

    lines = text[match.end():].split("\n")
    first = next((i for i, line in enumerate(lines) if line.strip()), None)
    title_match = _H1.match(lines[first]) if first is not None else None
    if not title_match:
        raise DocumentError(f"{source}: the first line after the front matter must be a '# Title'.")
    body = "\n".join(lines[first + 1:])
    _check_single_title(body, source)
    if not body.strip():
        raise DocumentError(f"{source}: the document has no content after its title.")
    return Document(kind=kind, doc_id=doc_id, source=source, title=title_match.group(1).strip(), body=body,
                    metadata=metadata)


def _refuse_fault_injection_terms(text: str, source: str) -> None:
    for number, line in enumerate(text.split("\n"), start=1):
        if filters.mentions_flags(line):
            raise DocumentError(
                f"{source}:{number}: mentions fault-injection terms. Knowledge documents must describe "
                "symptoms, checks and fixes only, never how faults are injected in the test environment.")


def _check_single_title(body: str, source: str) -> None:
    in_fence = False
    for line in body.split("\n"):
        if _FENCE.match(line):
            in_fence = not in_fence
        elif not in_fence and _H1.match(line):
            raise DocumentError(f"{source}: only one '# Title' is allowed; use '##' for sections.")


def _string_list(value: Any) -> bool:
    return isinstance(value, list) and bool(value) and all(isinstance(v, str) and v.strip() for v in value)


def _validate_metadata(meta: dict[str, Any], kind: str, source: str) -> dict[str, Any]:
    if not _string_list(meta.get("services")):
        raise DocumentError(f"{source}: front matter needs 'services', a non-empty list of service names.")
    if "tags" in meta and not _string_list(meta["tags"]):
        raise DocumentError(f"{source}: 'tags' must be a non-empty list of words.")
    if kind == "postmortem":
        for key in ("incident_id", "date", "severity", "root_cause_service"):
            if not isinstance(meta.get(key), str) or not meta[key].strip():
                raise DocumentError(f"{source}: postmortem front matter needs '{key}'.")
        if meta.get("failure_type") not in FAILURE_TYPES:
            raise DocumentError(f"{source}: 'failure_type' must be one of {sorted(FAILURE_TYPES)}.")
        minutes = meta.get("duration_minutes")
        if isinstance(minutes, bool) or not isinstance(minutes, int) or minutes < 0:
            raise DocumentError(f"{source}: 'duration_minutes' must be a whole number >= 0.")
    return meta


def _jsonable(value: Any) -> Any:
    """YAML turns 2026-03-14 into a date object; store everything as JSON-friendly values."""
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    if isinstance(value, dict):
        return {str(k): _jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(v) for v in value]
    return value


# --------------------------------------------------------------------------- #
# Chunking
# --------------------------------------------------------------------------- #
def chunk_document(doc: Document, max_chars: int = MAX_CHUNK_CHARS) -> list[Chunk]:
    keywords = " ".join([*doc.metadata.get("services", []), *doc.metadata.get("tags", [])])
    chunks: list[Chunk] = []
    for heading, text in split_sections(doc.body):
        parts = split_long(text, max_chars)
        for n, part in enumerate(parts, start=1):
            position = len(chunks)
            label = heading if len(parts) == 1 else f"{heading} (part {n})"
            chunks.append(Chunk(
                id=f"{doc.kind}/{doc.doc_id}#{position:02d}-{_slug(label)}",
                kind=doc.kind, doc_id=doc.doc_id, source=doc.source, title=doc.title, heading=label,
                position=position, text=part, keywords=keywords, metadata=doc.metadata,
            ))
    if not chunks:
        raise DocumentError(f"{doc.source}: no section has any text.")
    return chunks


def split_sections(body: str) -> list[tuple[str, str]]:
    """Split at "##" headings (outside code blocks). Returns (heading, text) pairs with text."""
    sections: list[tuple[str, list[str]]] = [(OVERVIEW, [])]
    in_fence = False
    for line in body.split("\n"):
        if _FENCE.match(line):
            in_fence = not in_fence
        heading = None if in_fence else _H2.match(line)
        if heading:
            sections.append((heading.group(1).strip(), []))
        else:
            sections[-1][1].append(line)
    return [(heading, "\n".join(lines).strip()) for heading, lines in sections if "\n".join(lines).strip()]


def split_long(text: str, max_chars: int = MAX_CHUNK_CHARS) -> list[str]:
    """Pack paragraphs (blank-line separated, code blocks kept whole) into parts of at most ``max_chars``."""
    if len(text) <= max_chars:
        return [text]
    parts: list[str] = []
    current = ""
    for paragraph in _paragraphs(text):
        for piece in _hard_split(paragraph, max_chars):
            candidate = f"{current}\n\n{piece}" if current else piece
            if len(candidate) <= max_chars:
                current = candidate
            else:
                parts.append(current)
                current = piece
    if current:
        parts.append(current)
    return parts


def _paragraphs(text: str) -> list[str]:
    paragraphs, current, in_fence = [], [], False
    for line in text.split("\n"):
        if _FENCE.match(line):
            in_fence = not in_fence
        if not line.strip() and not in_fence:
            if current:
                paragraphs.append("\n".join(current))
                current = []
        else:
            current.append(line)
    if current:
        paragraphs.append("\n".join(current))
    return paragraphs


def _hard_split(paragraph: str, max_chars: int) -> list[str]:
    """Last resort for a single paragraph longer than ``max_chars``: split at lines, then characters."""
    if len(paragraph) <= max_chars:
        return [paragraph]
    pieces, current = [], ""
    for line in paragraph.split("\n"):
        while len(line) > max_chars:
            if current:
                pieces.append(current)
                current = ""
            pieces.append(line[:max_chars])
            line = line[max_chars:]
        candidate = f"{current}\n{line}" if current else line
        if len(candidate) <= max_chars:
            current = candidate
        else:
            pieces.append(current)
            current = line
    if current:
        pieces.append(current)
    return pieces


def _slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")[:60] or "section"
