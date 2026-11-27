"""Send one alert to the diagnosis agent and print every step of its reasoning.

Usage (demo running, GOOGLE_API_KEY in .env):
    .venv/bin/python scripts/run_diagnosis.py
    .venv/bin/python scripts/run_diagnosis.py "checkout error ratio above 5% for 2 minutes"
    .venv/bin/python scripts/run_diagnosis.py --runs 5        # repeat to see run-to-run variation
    .venv/bin/python scripts/run_diagnosis.py --knowledge     # also search runbooks and postmortems (Week 3)

Each run is saved to runs/<timestamp>-diagnosis.json (alert, steps, answer,
tokens, latency) so you can compare runs and paste transcripts when debugging.
"""

from __future__ import annotations

import argparse
import os
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv  # noqa: E402

from copilot.agents.console import print_step, save_run  # noqa: E402
from copilot.agents.diagnosis import build_diagnosis_agent, diagnose  # noqa: E402

DEFAULT_ALERT = ("ALERT: users report failures at checkout in the last few minutes. "
                 "Investigate and find the root cause.")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("alert", nargs="?", default=DEFAULT_ALERT, help="alert text given to the agent")
    parser.add_argument("--runs", type=int, default=1, help="repeat the investigation N times")
    parser.add_argument("--model", default=None, help="override $COPILOT_MODEL")
    parser.add_argument("--knowledge", action="store_true",
                        help="add search_runbooks/search_postmortems (needs the ingested knowledge base)")
    args = parser.parse_args()

    load_dotenv(ROOT / ".env")
    if not (os.environ.get("GOOGLE_API_KEY") or os.environ.get("GOOGLE_GENAI_USE_VERTEXAI")):
        print("GOOGLE_API_KEY is not set. Copy .env.example to .env and add your AI Studio key.", file=sys.stderr)
        return 2

    answers = Counter()
    for i in range(1, args.runs + 1):
        print(f"\n{'=' * 78}\nRUN {i}/{args.runs}   alert: {args.alert}\n{'=' * 78}")
        agent = build_diagnosis_agent(model=args.model, knowledge=args.knowledge)
        run = diagnose(args.alert, agent=agent, on_step=print_step)
        path = save_run(run.to_dict(), prefix="diagnosis-knowledge" if args.knowledge else "diagnosis")
        print("-" * 78)
        if run.error:
            print(f"ERROR: {run.error}")
        if run.diagnosis is None and run.raw_answer:
            print("Unparsed answer:\n" + run.raw_answer[:2000])
        print(f"model {run.model} | {run.tool_calls} tool calls | {run.tokens['total']} tokens "
              f"| {run.latency_seconds:.1f} s | saved {path.relative_to(ROOT)}")
        answers[run.diagnosis.root_cause_service if run.diagnosis else "(no answer)"] += 1

    if args.runs > 1:
        print("\nRoot cause across runs: " + ", ".join(f"{k} x{v}" for k, v in answers.most_common()))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
