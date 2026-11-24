"""Agent prompts live here as Markdown files, versioned and reviewed like code."""

from __future__ import annotations

from pathlib import Path

PROMPT_DIR = Path(__file__).resolve().parent


def load_prompt(name: str) -> str:
    """load_prompt("diagnosis") -> contents of copilot/prompts/diagnosis.md."""
    if not name.isidentifier():
        raise ValueError(f"invalid prompt name: {name!r}")
    return (PROMPT_DIR / f"{name}.md").read_text(encoding="utf-8")
