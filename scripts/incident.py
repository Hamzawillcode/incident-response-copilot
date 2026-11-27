"""Run the incident team on an alert, then approve or reject the action it proposes (Week 4).

Usage (demo running, kb-postgres running and ingested, GOOGLE_API_KEY in .env):
    .venv/bin/python scripts/incident.py run                         # default alert, every step printed
    .venv/bin/python scripts/incident.py run "checkout error ratio above 5%" --interactive
    .venv/bin/python scripts/incident.py approve inc-20261128-0a1b2c [--note "..."] [--verify-minutes 8]
    .venv/bin/python scripts/incident.py reject  inc-20261128-0a1b2c --note "wrong service"
    .venv/bin/python scripts/incident.py list
    .venv/bin/python scripts/incident.py show    inc-20261128-0a1b2c [--json]

`run` stops when the action agent proposes a change: the incident is then "awaiting_approval"
in Postgres, and nothing has been changed. Approve or reject it later, from any terminal, with
`approve` / `reject`; ADK resumes the paused action from the session stored in Postgres.
`--interactive` asks right away instead.

Every incident is also saved to runs/<timestamp>-incident-<id>.json.
"""

from __future__ import annotations

import argparse
import asyncio
import getpass
import json
import logging
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv  # noqa: E402

from copilot.agents.console import format_incident, print_team_step, save_run  # noqa: E402
from copilot.kb.store import StoreError  # noqa: E402
from copilot.state import IncidentStore, StateError  # noqa: E402

DEFAULT_ALERT = ("ALERT: users report failures at checkout in the last few minutes. "
                 "Investigate and find the root cause.")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)
    run = sub.add_parser("run", help="investigate an alert with the agent team")
    run.add_argument("alert", nargs="?", default=DEFAULT_ALERT)
    run.add_argument("--interactive", action="store_true", help="ask for approval right away")
    run.add_argument("--max-review-rounds", type=int, default=2)
    run.add_argument("--verify-minutes", type=float, default=0.0, help="with --interactive: check recovery")
    for name in ("approve", "reject"):
        p = sub.add_parser(name, help=f"{name} the proposed action of an incident")
        p.add_argument("incident_id")
        p.add_argument("--by", default=getpass.getuser(), help="your name (default: your login)")
        p.add_argument("--note", default="")
        if name == "approve":
            p.add_argument("--verify-minutes", type=float, default=0.0,
                           help="after the fix, poll error ratios for up to N minutes (0 = skip)")
    lst = sub.add_parser("list", help="recent incidents")
    lst.add_argument("--limit", type=int, default=20)
    show = sub.add_parser("show", help="one incident")
    show.add_argument("incident_id")
    show.add_argument("--json", action="store_true", help="print the full record")
    parser.add_argument("--debug", action="store_true", help="show ADK's own error tracebacks")
    args = parser.parse_args()
    if not args.debug:
        # ADK logs every model error with a full traceback; the incident record keeps the error anyway.
        logging.getLogger("google_adk").setLevel(logging.CRITICAL)

    load_dotenv(ROOT / ".env")
    try:
        if args.command == "list":
            return _list(args.limit)
        if args.command == "show":
            return _show(args.incident_id, args.json)
        if not (os.environ.get("GOOGLE_API_KEY") or os.environ.get("GOOGLE_GENAI_USE_VERTEXAI")):
            print("GOOGLE_API_KEY is not set. Add it to .env.", file=sys.stderr)
            return 2
        return asyncio.run(_agents(args))
    except (StoreError, StateError) as exc:
        print(f"error: {getattr(exc, 'message', exc)}", file=sys.stderr)
        return 3


async def _agents(args) -> int:
    from copilot.agents.coordinator import IncidentCoordinator
    from copilot.telemetry import setup_tracing, shutdown_tracing

    traced = setup_tracing()
    if traced:
        print(f"tracing to {traced} (Jaeger UI: http://localhost:16687)")
    coordinator = IncidentCoordinator(on_step=print_team_step,
                                      max_review_rounds=getattr(args, "max_review_rounds", 2))
    try:
        if args.command == "run":
            print(f"{'=' * 78}\n{args.alert}\n{'=' * 78}")
            record = await coordinator.investigate(args.alert)
            _report(record)
            if record.status == "awaiting_approval" and args.interactive:
                answer = input("\nApprove this action? [a]pprove / [r]eject / Enter = decide later: ").strip().lower()
                if answer in ("a", "approve", "y", "yes"):
                    record = await coordinator.decide(record.id, approve=True, by=getpass.getuser(),
                                                      verify_minutes=args.verify_minutes)
                    _report(record)
                elif answer in ("r", "reject", "n", "no"):
                    record = await coordinator.decide(record.id, approve=False, by=getpass.getuser())
                    _report(record)
            if record.status == "awaiting_approval":
                print(f"\nNothing has been changed. Decide with:\n"
                      f"  .venv/bin/python scripts/incident.py approve {record.id}\n"
                      f"  .venv/bin/python scripts/incident.py reject  {record.id} --note \"why\"")
        else:
            record = await coordinator.decide(args.incident_id, approve=args.command == "approve", by=args.by,
                                              note=args.note, verify_minutes=getattr(args, "verify_minutes", 0.0))
            _report(record)
    finally:
        await coordinator.close()
        if traced:
            shutdown_tracing()
    return 0


def _report(record) -> None:
    data = record.model_dump(mode="json")
    path = save_run({**data, "totals": record.totals()}, prefix=f"incident-{record.id}")
    print(f"{'-' * 78}\n{format_incident(data)}\n  saved:      {path.relative_to(ROOT)}")


def _list(limit: int) -> int:
    records = IncidentStore().list(limit)
    if not records:
        print("No incidents yet.")
    for r in records:
        d = r.diagnosis or {}
        print(f"{r.id}  {r.created_at[:19]}  {r.status:<18} {d.get('root_cause_service', '-'):<16} "
              f"{d.get('failure_type', '-')}")
    return 0


def _show(incident_id: str, as_json: bool) -> int:
    record = IncidentStore().get(incident_id)
    data = record.model_dump(mode="json")
    print(json.dumps(data, indent=2) if as_json else format_incident(data))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
