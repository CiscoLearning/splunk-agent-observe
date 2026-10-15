"""tshoot-agent: a read-only network troubleshooting agent, traced in Splunk Agent Observability.

Run it with:  uv run --env-file .env tshoot-agent.py
"""

import asyncio
import os
from functools import partial
from pathlib import Path

from langchain_aws import ChatBedrockConverse
from langchain_core.messages import HumanMessage, SystemMessage
from langchain_core.runnables import RunnableConfig, RunnableLambda
from langchain_mcp_adapters.client import MultiServerMCPClient
from langchain_mcp_adapters.tools import load_mcp_tools
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.graph import START, StateGraph
from langgraph.prebuilt import ToolNode, tools_condition
from splunk_ao import SplunkAOLogger
from splunk_ao.handlers.langchain import SplunkAOAsyncCallback

from controls import guard, init_controls
from memory import AgentState, check_memory, make_summarizer, with_summary
from tshoot_ui import TshootApp

# --- 1. System prompt: an intent and a method, not a script (see system-prompt.md) ----------

SYSTEM_PROMPT = Path(__file__).with_name("system-prompt.md").read_text()

# --- 2. Tools: the CML MCP server -----------------------------------------------------------


# The CML MCP server runs on the CML server; the agent connects to it over HTTPS (settings in .env).
CML_SERVER = {
    "transport": "streamable_http",
    "url": os.environ["CML_MCP_URL"],
    "headers": {
        "X-Authorization": os.environ["CML_AUTH_HEADER"],
        "X-PyATS-Authorization": os.environ["PYATS_AUTH_HEADER"],
        "X-PyATS-Enable": os.environ["PYATS_ENABLE_HEADER"],
    },
}


# Gets the tools from one open MCP session: the read-only tools, plus send_cli_command to run CLI.
async def load_tools(session) -> list:
    mcp_tools = await load_mcp_tools(session)
    # The MCP server marks each tool readOnlyHint; the model only gets the read-only ones.
    read_only = [t for t in mcp_tools if t.metadata.get("readOnlyHint")]
    # send_cli_command can also change config; only the system prompt keeps the agent read-only.
    send_cli_command = next(t for t in mcp_tools if t.name == "send_cli_command")
    # Every tool call is checked against the Controls in Splunk (see controls.py).
    return [guard(t) for t in read_only + [send_cli_command]]


# --- 3. The LangGraph agent: model -> tools -> model, until the model answers ---------------


# Builds the graph: the model picks tools, the tools run, and the loop repeats until it answers.
# With /memory on, a summarize step first condenses older turns (see memory.py).
def build_agent(tools: list):
    llm = ChatBedrockConverse(
        model_id=os.environ["BEDROCK_MODEL_ID"],
        region_name=os.environ["AWS_REGION"],
        credentials_profile_name=os.getenv("AWS_PROFILE"),
        max_tokens=4000,
    )
    model = llm.bind_tools(tools)

    # One model call: the system prompt (plus any memory summary) and the conversation so far.
    async def agent(state: AgentState):
        messages = [SystemMessage(with_summary(SYSTEM_PROMPT, state))] + state["messages"]
        return {"messages": [await model.ainvoke(messages)]}

    graph = StateGraph(AgentState)
    graph.add_node("summarize", make_summarizer(llm))
    graph.add_node("agent", agent)
    graph.add_node("tools", ToolNode(tools, handle_tool_errors=True))  # errors go back to model
    graph.add_conditional_edges(START, check_memory, ["summarize", "agent"])  # memory check
    graph.add_edge("summarize", "agent")
    graph.add_conditional_edges("agent", tools_condition)  # tool calls -> "tools", else end
    graph.add_edge("tools", "agent")
    return graph.compile(checkpointer=InMemorySaver())  # memory, so follow-ups work


# Asks one question and hands each graph step to show(). Splunk traces this function as the
# root, so each trace shows the question as Input and the final answer as Output.
async def ask(
    agent, question: str, config: dict, show, memory: bool = False, *, splunk_logger, sessions: dict
) -> str:
    # One Splunk session per run and memory mode, so the Sessions tab shows on vs off side by side.
    # The active session is per async task and each question runs in a new task, so set it each time.
    mode = "on" if memory else "off"
    if mode in sessions:
        splunk_logger.set_session(sessions[mode])
    else:
        sessions[mode] = await splunk_logger.async_start_session(
            name=f"tshoot-agent · memory {mode}", metadata={"memory": mode}
        )

    config = {
        **config,
        "configurable": {**config["configurable"], "memory": memory},
        "metadata": {"memory": mode},  # to compare runs in Splunk
    }

    async def turn(question: str, config: RunnableConfig) -> str:
        answer = ""
        steps = agent.astream({"messages": [HumanMessage(question)]}, config, stream_mode="updates")
        async for step in steps:
            await show(step)
            for message in step.get("agent", {}).get("messages", []):
                if not message.tool_calls:
                    answer = message.text
        return answer

    return await RunnableLambda(turn).ainvoke(question, config)


# --- 4. Run it, with every step traced in Splunk Agent Observability ------------------------


# Turns on Splunk tracing and Controls, opens one CML MCP session, builds the agent, opens the UI.
async def main() -> None:
    # Splunk: the project comes from SPLUNK_AO_PROJECT; Controls come from this agent stream.
    splunk_logger = SplunkAOLogger(agent_stream="tshoot-agent")
    init_controls(splunk_logger.agent_stream_id)

    # One session for the whole run, so tool calls don't each start a new MCP connection.
    client = MultiServerMCPClient({"cml": CML_SERVER})
    async with client.session("cml") as session:
        tools = await load_tools(session)
        agent = build_agent(tools)

        # The callback traces every step to Splunk.
        splunk_callback = SplunkAOAsyncCallback(
            splunk_ao_logger=splunk_logger, flush_on_chain_end=True
        )

        config = {
            "configurable": {"thread_id": "conversation-1"},  # which conversation to remember
            "callbacks": [splunk_callback],
            "run_name": "tshoot-agent",
            "recursion_limit": 40,  # stops runaway loops (one tool round = 2 steps)
        }
        ask_agent = partial(ask, agent, config=config, splunk_logger=splunk_logger, sessions={})
        await TshootApp(ask_agent, len(tools)).run_async()


if __name__ == "__main__":
    asyncio.run(main())
