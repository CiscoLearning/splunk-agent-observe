"""The terminal UI for tshoot-agent (Textual): logo, each step as it happens, then the report.

Styling lives in tshoot_ui.tcss.
"""

import os
import time

from langgraph.errors import GraphRecursionError
from rich.markup import escape
from textual.app import App, ComposeResult
from textual.containers import Horizontal, VerticalScroll
from textual.widgets import Input, Markdown, Static

ACCENT = "#d97757"
LOGO = r"""
 ●──────┐
        │
 ████████╗ ███████╗ ██╗  ██╗  ██████╗   ██████╗  ████████╗
 ╚══██╔══╝ ██╔════╝ ██║  ██║ ██╔═══██╗ ██╔═══██╗ ╚══██╔══╝
    ██║    ███████╗ ███████║ ██║   ██║ ██║   ██║    ██║────●
    ██║    ╚════██║ ██╔══██║ ██║   ██║ ██║   ██║    ██║
    ██║    ███████║ ██║  ██║ ╚██████╔╝ ╚██████╔╝    ██║────○
    ╚═╝    ╚══════╝ ╚═╝  ╚═╝  ╚═════╝   ╚═════╝     ╚═╝
 ●────────────────────────────────────────────── agent >
"""

PREFIXES = {
    "thought": "[bold]●[/] ",
    "tool": "[bold green]⏺[/] ",
    "result": "  ⎿  ",
    "error": "  ⎿  ",
    "memory": f"[bold {ACCENT}]◆[/] ",
}


# Short label for a tool call: send_cli_command(CORE-01 · show ip bgp) or get_cml_labs().
def describe(call: dict) -> str:
    args = call.get("args", {})
    if call.get("name") == "send_cli_command":
        mode = " · CONFIG" if args.get("config_command") else ""
        return f"send_cli_command({args.get('label')} · {args.get('commands')}{mode})"
    shown = ", ".join(str(v) for k, v in args.items() if k not in ("lab_id", "lid"))
    return f"{call.get('name')}({shown})"


class TshootApp(App):
    CSS_PATH = "tshoot_ui.tcss"
    AUTO_FOCUS = "Input"

    # ask_agent(question, show=...) runs one question; show() gets each step as it happens.
    def __init__(self, ask_agent, tool_count: int) -> None:
        super().__init__()
        self.ask_agent, self.tool_count = ask_agent, tool_count
        self.memory = False

    # Lays out the screen: logo and welcome box, status line, input box, footer.
    def compose(self) -> ComposeResult:
        welcome = (
            f"[bold {ACCENT}]✻[/] [bold]Welcome Human! My Services Are At Your Disposal![/]\n\n"
            "  [dim]Read-only network troubleshooting, traced in Splunk Agent Observability[/]\n\n"
            f"  [dim]model[/]   {os.environ['BEDROCK_MODEL_ID']} · AWS Bedrock\n"
            f"  [dim]tools[/]   CML MCP · {self.tool_count} tools\n"
            f"  [dim]trace[/]   Splunk · {os.environ['SPLUNK_AO_PROJECT']} / tshoot-agent"
        )
        with VerticalScroll(id="log"):
            yield Static(LOGO, id="logo")
            yield Static(welcome, id="welcome")
        yield Static("", id="status")
        yield Input(placeholder="Ask about the network…")
        with Horizontal(id="footer"):
            yield Static(self.footer_text(), id="footer-left")
            yield Static(f"[{ACCENT}]●[/] Splunk tracing · tshoot-agent", id="footer-right")

    # Footer hint, including whether memory summarization is on.
    def footer_text(self) -> str:
        state = f"[{ACCENT}]on[/]" if self.memory else "off"
        return f"/memory {state} · exit to quit · ctrl+q"

    # Adds a widget to the conversation and scrolls to it.
    async def show(self, widget) -> None:
        log = self.query_one("#log")
        await log.mount(widget)
        log.scroll_end(animate=False)

    # Adds one styled line: a thought, a tool call, its result, or an error.
    async def line(self, kind: str, text: str) -> None:
        await self.show(Static(PREFIXES[kind] + escape(text), classes=kind))

    # Handles Enter in the input box: quit, set memory (/memory toggles, /memory on|off sets it),
    # or ask the agent in the background.
    async def on_input_submitted(self, event: Input.Submitted) -> None:
        question = event.value.strip()
        event.input.value = ""
        if question.lower() in {"exit", "quit", "q", "/exit", "/quit", "/q"}:
            self.exit()
        elif question.lower() in {"/memory", "/memory on", "/memory off"}:
            command = question.lower()
            self.memory = (not self.memory) if command == "/memory" else command == "/memory on"
            self.query_one("#footer-left", Static).update(self.footer_text())
            if self.memory:
                await self.line(
                    "memory", "Memory on: earlier turns get summarized before each question."
                )
            else:
                await self.line("memory", "Memory off: the full conversation is sent each time.")
        elif question:
            await self.show(Static(f"> {escape(question)}", classes="user"))
            self.run_worker(self.ask(question), exclusive=True)

    # Runs one question with the input box locked, then shows a summary in the status line.
    async def ask(self, question: str) -> None:
        status, box = self.query_one("#status", Static), self.query_one(Input)
        box.disabled, self.start, self.calls, self.pending = True, time.monotonic(), 0, {}
        status.update("✻ Thinking…")
        try:
            await self.ask_agent(question, show=self.show_step, memory=self.memory)
        except GraphRecursionError:
            await self.line("error", "Stopped: step limit reached.")
        elapsed = time.monotonic() - self.start
        status.update(f"[dim]✻ Done · {self.calls} tool calls · {elapsed:.0f}s[/]")
        box.disabled = False
        box.focus()

    # Shows one graph step: a memory summary, the model's thought and tool calls, the tool
    # results, or the report.
    async def show_step(self, step: dict) -> None:
        status = self.query_one("#status", Static)
        if memory := step.get("summarize"):
            summary_tokens = len(memory["summary"]) // 4
            await self.line(
                "memory",
                f"Condensed {len(memory['messages'])} earlier messages "
                f"(≈{memory['condensed_tokens']:,} tokens) into a summary (≈{summary_tokens:,} tokens)",
            )
        for message in step.get("agent", {}).get("messages", []):
            if not message.tool_calls:
                await self.show(Markdown(message.text))
                continue
            if message.text.strip():
                await self.line("thought", message.text.strip())
            self.pending = {call["id"]: call for call in message.tool_calls}
            status.update(f"✻ Running {', '.join(map(describe, self.pending.values()))}…")
        for message in step.get("tools", {}).get("messages", []):
            self.calls += 1
            text = message.text.strip()
            await self.line("tool", describe(self.pending.get(message.tool_call_id, {})))
            if message.status == "error":
                await self.line("error", text[:100])
            else:
                await self.line("result", f"{len(text.splitlines())} lines")
            elapsed = time.monotonic() - self.start
            status.update(f"✻ Thinking… ({elapsed:.0f}s · {self.calls} tool calls)")
