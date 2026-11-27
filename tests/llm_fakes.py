"""A scripted fake model for driving real ADK agents in tests (no Gemini, no network).

    llm = scripted({"call": "get_service_health", "args": {"minutes": 15}}, {"incident": True, ...})

Each model request returns the next turn: a dict with "call" becomes a function call, any
other dict or string becomes the text answer, and an Exception is raised. After the script
ends, the last turn repeats. ``llm.requests`` keeps every request the agent sent, so a test
can inspect what the model was told.

The same ScriptedLlm instance can serve several sessions in a row (e.g. two diagnosis rounds):
the script simply continues.
"""

from __future__ import annotations

import json

from google.adk.models import BaseLlm, LlmCapabilities, LlmResponse
from google.genai import types


class ScriptedLlm(BaseLlm):
    model: str = "scripted"
    script: list = []
    requests: list = []
    native_schema: bool = True

    @property
    def capabilities(self) -> LlmCapabilities:
        return LlmCapabilities(output_schema_and_tools=self.native_schema)

    async def generate_content_async(self, llm_request, stream=False):
        self.requests.append(llm_request)
        turn = self.script[min(len(self.requests) - 1, len(self.script) - 1)]
        if isinstance(turn, Exception):
            raise turn
        if isinstance(turn, dict) and "call" in turn:
            part = types.Part(function_call=types.FunctionCall(name=turn["call"], args=turn.get("args", {})))
        else:
            part = types.Part(text=turn if isinstance(turn, str) else json.dumps(turn))
        usage = types.GenerateContentResponseUsageMetadata(prompt_token_count=100, candidates_token_count=10,
                                                           total_token_count=110)
        yield LlmResponse(content=types.Content(role="model", parts=[part]), usage_metadata=usage)


def scripted(*turns) -> ScriptedLlm:
    return ScriptedLlm(script=list(turns), requests=[])


def request_text(llm_request) -> str:
    """All text the model received in one request (system instruction excluded)."""
    parts = [p.text for c in llm_request.contents or [] for p in c.parts or [] if p.text]
    return "\n".join(parts)
