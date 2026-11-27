# Knowledge base

The written knowledge the copilot searches with `search_runbooks` and `search_postmortems`
(RAG). Only `runbooks/*.md` and `postmortems/*.md` are ingested; this README is not.

```
kb/
  runbooks/      how to investigate and fix problems, one file per service area (14)
  postmortems/   write-ups of past incidents (5 seed documents)
```

All documents were written for this project. The postmortems are **made up**: they describe
plausible past incidents of the demo shop so the copilot has history to search. They are
seed data, not records of real events.

## Format

Every file starts with YAML front matter, then one `# Title`, then `## Sections`. Ingestion
splits each file at its `##` headings, so every section should make sense on its own.

Runbook front matter:

```yaml
services: [payment]               # services the runbook is about (required)
tags: [charge, card, billing]     # extra search words (optional)
owner: payments-team              # optional
last_reviewed: 2026-10-15         # optional
```

Postmortem front matter (all required):

```yaml
incident_id: INC-2026-0314
date: 2026-03-14
severity: SEV-2
services: [payment, checkout, frontend]
root_cause_service: payment
failure_type: errors              # one of the copilot's failure types
duration_minutes: 47
```

## Rules

- Describe **symptoms, checks and fixes** as an engineer would. Never mention how faults are
  injected in the test environment. Ingestion refuses any document containing
  fault-injection terms, and a unit test scans every file.
- Keep each `##` section under about 1,500 characters. Longer sections are split at blank
  lines.
- After editing, run `python -m copilot.kb.ingest`. Only changed sections are re-embedded.
