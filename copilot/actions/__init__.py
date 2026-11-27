"""Remediation tools (Week 4): the only code in the copilot that changes the system.

* Only the Action agent gets these tools. The investigation agents get ``copilot.tools``,
  which is read-only and never imports this package (a test checks this).
* ``rollback_config`` and ``restart_service`` are wrapped with ADK tool confirmation
  (``require_confirmation=True``): when the agent calls one, the run pauses and nothing
  happens until a human approves that exact call.
* ``escalate_to_human`` changes nothing, so it needs no approval.
"""

from google.adk.tools import FunctionTool

from copilot.actions.remediation import (
    RESTARTABLE_SERVICES, ROLLBACK_SERVICES, escalate_to_human, restart_service, rollback_config,
)

#: Tools that change the system. Each call needs a human's approval first.
WRITE_TOOL_NAMES = frozenset({"rollback_config", "restart_service"})

#: The Action agent's toolset, in the form ADK needs (confirmation on the write tools).
ACTION_TOOLS = (
    FunctionTool(rollback_config, require_confirmation=True),
    FunctionTool(restart_service, require_confirmation=True),
    FunctionTool(escalate_to_human),
)

__all__ = ["ACTION_TOOLS", "RESTARTABLE_SERVICES", "ROLLBACK_SERVICES", "WRITE_TOOL_NAMES", "escalate_to_human",
           "restart_service", "rollback_config"]
