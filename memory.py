"""Memory for tshoot-agent: folds older turns into a running summary (/memory in the UI).

Before each question, if memory is on and the earlier conversation passes MEMORY_TOKEN_LIMIT
tokens, a summarize node merges it into the summary and drops those messages from the state.
"""

import os

from langchain_core.messages import (
    AIMessage,
    HumanMessage,
    RemoveMessage,
    SystemMessage,
    ToolMessage,
)
from langchain_core.messages.utils import count_tokens_approximately
from langchain_core.runnables import RunnableConfig
from langgraph.graph import MessagesState

MEMORY_TOKEN_LIMIT = int(os.getenv("MEMORY_TOKEN_LIMIT", "2000"))

SUMMARY_PROMPT = """You maintain the memory of a network troubleshooting agent.
Merge the conversation below into the existing summary. Keep facts the agent may need later:
the lab, device names, commands run, findings, root causes, proposed fixes, and what the user
asked for. Drop raw command output. Reply with the updated summary only, as short bullet points."""


# The agent's state: the conversation, plus the summary that replaces older turns.
class AgentState(MessagesState):
    summary: str
    condensed_tokens: int


# Flattens messages to plain text for the summarizer, which has no tools to read tool calls.
def transcript(messages: list) -> str:
    lines = []
    for m in messages:
        if isinstance(m, HumanMessage):
            lines.append(f"User: {m.text}")
        elif isinstance(m, AIMessage):
            if m.text.strip():
                lines.append(f"Agent: {m.text}")
            lines += [f"Agent ran {c['name']}({c['args']})" for c in m.tool_calls]
        elif isinstance(m, ToolMessage):
            lines.append(f"Tool result: {m.text}")
    return "\n".join(lines)


# Before each question: summarize first if memory is on and the earlier turns are too long.
def check_memory(state: AgentState, config: RunnableConfig) -> str:
    earlier = state["messages"][:-1]
    too_long = count_tokens_approximately(earlier) > MEMORY_TOKEN_LIMIT
    return "summarize" if config["configurable"].get("memory") and too_long else "agent"


# Builds the summarize node: folds every turn before the new question into the summary, then
# drops those messages.
def make_summarizer(llm):
    async def summarize(state: AgentState):
        earlier = state["messages"][:-1]
        request = (
            f"Existing summary:\n{state.get('summary') or '(none)'}\n\n"
            f"Conversation:\n{transcript(earlier)}"
        )
        reply = await llm.ainvoke([SystemMessage(SUMMARY_PROMPT), HumanMessage(request)])
        return {
            "summary": reply.text,
            "condensed_tokens": count_tokens_approximately(earlier),
            "messages": [RemoveMessage(id=m.id) for m in earlier],
        }

    return summarize


# Adds the summary, if there is one, to the end of the system prompt.
def with_summary(system_prompt: str, state: AgentState) -> str:
    if not state.get("summary"):
        return system_prompt
    return f"{system_prompt}\n\n## Earlier in this conversation\n{state['summary']}"
