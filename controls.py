"""Agent Control: Splunk checks each tool call against the Controls on the agent stream.

The rules (what to block) live in Splunk's Controls tab, so they change without touching code.
"""

import os

import agent_control
from agent_control import ControlViolationError, control
from langchain_core.tools import BaseTool, StructuredTool


# Connects to Agent Control and picks up the Controls attached to this agent stream.
def init_controls(agent_stream_id: str) -> None:
    agent_control.init(
        agent_name="tshoot-agent",
        server_url=os.environ["AGENT_CONTROL_URL"],
        api_key=os.environ["SPLUNK_AO_API_KEY"],
        api_key_header="Galileo-API-Key",
        target_type="log_stream",
        target_id=agent_stream_id,
    )


# Wraps a tool so Splunk checks its input before it runs and its output after. On a Deny, the
# model gets "Blocked by policy" instead of the output.
def guard(tool: BaseTool) -> BaseTool:
    @control(step_name=tool.name, step_type="tool")
    async def checked(**kwargs) -> str:
        # No callbacks: the wrapper is already traced, so the inner call would log a duplicate span.
        result = await tool.ainvoke(kwargs, config={"callbacks": []})
        return (
            "\n".join(block.get("text", "") for block in result)
            if isinstance(result, list)
            else str(result)
        )

    async def run(**kwargs) -> str:
        try:
            return await checked(**kwargs)
        except ControlViolationError as e:
            return f"Blocked by policy ({e.control_name}): {e.message}"

    return StructuredTool.from_function(
        coroutine=run, name=tool.name, description=tool.description, args_schema=tool.args_schema
    )
