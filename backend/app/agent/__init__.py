"""Agent package: tool registry and the tool-calling loop."""
from .loop import AgentResult, run_agent
from .tools import TOOL_SCHEMAS, ToolContext, execute_tool

__all__ = ["AgentResult", "run_agent", "TOOL_SCHEMAS", "ToolContext", "execute_tool"]
