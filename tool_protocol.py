from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from typing import Iterable


EXIT_RE = re.compile(r"~@exit\d*@~", re.IGNORECASE)
START_RE = re.compile(r"~@(?P<name>[A-Za-z_][A-Za-z0-9_:-]*)(?P<number>\d*)@~", re.IGNORECASE)
TRANSPORT_MARKERS = {"agent_reply", "gemini_reply", "ready_turn", "coder_reply"}
TOOL_ALIASES = {
    "read_file": "read",
    "file_read": "read",
    "open_file": "read",
    "cat": "read",
    "mkdri": "mkdir",
    "md": "mkdir",
    "makedir": "mkdir",
    "make_dir": "mkdir",
    "create_folder": "mkdir",
    "folder_create": "mkdir",
    "dir": "explorer",
    "list_dir": "explorer",
    "ls": "explorer",
    "remove": "delete",
    "rm": "delete",
    "del": "delete",
    "ps": "powershell",
    "shell": "terminal",
}


@dataclass(frozen=True)
class ToolCall:
    name: str
    raw: str
    marker: str
    index: int

    @property
    def signature(self) -> str:
        payload = f"{self.name}\n{self.raw}".encode("utf-8", errors="replace")
        return hashlib.sha256(payload).hexdigest()


@dataclass(frozen=True)
class ParsedReply:
    chat_text: str
    tool_calls: list[ToolCall]

    @property
    def has_tools(self) -> bool:
        return bool(self.tool_calls)


def parse_tool_calls(text: str) -> list[ToolCall]:
    value = str(text or "")
    calls: list[ToolCall] = []
    pos = 0
    while True:
        start = START_RE.search(value, pos)
        if not start:
            break
        name = normalize_tool_name(start.group("name"))
        marker = start.group(0)
        end = EXIT_RE.search(value, start.end())
        if not end:
            break
        raw = value[start.end() : end.start()].strip()
        pos = end.end()
        if name in TRANSPORT_MARKERS:
            continue
        calls.append(ToolCall(name=name, raw=raw, marker=marker, index=len(calls)))
    return calls


def normalize_tool_name(name: str) -> str:
    value = str(name or "").strip().lower()
    return TOOL_ALIASES.get(value, value)


def strip_tool_blocks(text: str) -> str:
    value = str(text or "")
    chunks: list[str] = []
    pos = 0
    while True:
        start = START_RE.search(value, pos)
        if not start:
            chunks.append(value[pos:])
            break
        end = EXIT_RE.search(value, start.end())
        if not end:
            chunks.append(value[pos:])
            break
        name = start.group("name").lower()
        if name in TRANSPORT_MARKERS:
            chunks.append(value[pos : start.start()])
            chunks.append(value[start.end() : end.start()])
        else:
            chunks.append(value[pos : start.start()])
        pos = end.end()
    return normalize_text("\n".join(part.strip() for part in chunks if part.strip()))


def parse_reply(text: str) -> ParsedReply:
    return ParsedReply(chat_text=strip_tool_blocks(text), tool_calls=parse_tool_calls(text))


def normalize_text(text: str) -> str:
    value = str(text or "").replace("\r\n", "\n").replace("\r", "\n")
    value = value.replace("\xa0", " ")
    value = re.sub(r"[ \t]+\n", "\n", value)
    value = re.sub(r"\n{3,}", "\n\n", value)
    return value.strip()


def first_line_and_body(raw: str) -> tuple[str, str]:
    lines = str(raw or "").replace("\r\n", "\n").replace("\r", "\n").split("\n", 1)
    first = lines[0].strip() if lines else ""
    body = lines[1] if len(lines) > 1 else ""
    return first, body


def parse_key_value_block(raw: str) -> dict[str, str]:
    data: dict[str, str] = {}
    current_key = ""
    current_lines: list[str] = []
    for line in str(raw or "").replace("\r\n", "\n").replace("\r", "\n").splitlines():
        match = re.match(r"^([A-Za-z_][A-Za-z0-9_-]*)\s*:\s*(.*)$", line)
        if match:
            if current_key:
                data[current_key] = "\n".join(current_lines).strip()
            current_key = match.group(1).lower()
            current_lines = [match.group(2)]
        elif current_key:
            current_lines.append(line)
    if current_key:
        data[current_key] = "\n".join(current_lines).strip()
    return data


def parse_replace_payload(raw: str) -> tuple[str, str, str]:
    path, body = first_line_and_body(raw)
    if "---OLD---" not in body or "---NEW---" not in body:
        raise ValueError("replace requires ---OLD--- and ---NEW--- sections")
    old_part = body.split("---OLD---", 1)[1]
    old, new = old_part.split("---NEW---", 1)
    return path, old.strip("\n"), new.strip("\n")


def build_tool_result_message(turn_id: str, results: Iterable[tuple[ToolCall, str]]) -> str:
    lines = [
        f"TOOL RESULTS FOR TURN {turn_id}",
        "Use these results to continue. If more local work is needed, call another tool. If complete, answer normally without tool sigils.",
        "",
    ]
    for call, result in results:
        lines.append(f"[TOOL_RESULT:{call.name}:{call.index}]")
        lines.append(str(result or "").strip() or "[OK] Tool completed with no output.")
        lines.append("")
    return "\n".join(lines).strip()


def render_tool_contract(
    access_mode: str = "restricted",
    approval_policy: str = "guarded",
    roots: Iterable[str] | None = None,
    enabled_tools: Iterable[str] | None = None,
) -> str:
    root_lines = "\n".join(f"- {root}" for root in (roots or [])) or "- [configured in the desktop app]"
    tool_lines = ", ".join(sorted(enabled_tools or [])) or "all configured tools"
    return (
        TOOL_CONTRACT
        + "\n\nCURRENT LOCAL RUNTIME:\n"
        + f"- Filesystem access mode: {access_mode}\n"
        + f"- Approval policy: {approval_policy}\n"
        + f"- Enabled tools: {tool_lines}\n"
        + "- Allowed roots:\n"
        + root_lines
    )


TOOL_CONTRACT = """GEMINI LOCAL AGENT TOOL BOOTSTRAP

You are Gemini running inside Gemini Local Agent.

You are the primary agent brain. The desktop app can execute local tools for you.
Use normal reasoning in Gemini, including Canvas when helpful for UI/design work.

IMPORTANT:
- You DO have access to the user's computer through the local desktop bridge.
- Your access is not direct. Your hands are the local sigil tools listed below.
- Do not answer local-action requests with manual instructions like "press Ctrl+Shift+N".
- If the user asks you to create, edit, delete, inspect, click, type, run, browse, use Excel, control Blender, or control an app, call the proper tool.
- You can read files. Use ~@read@~ with the exact path. Use ~@explorer@~ first when you need to discover a file path.
- Reads inside allowed roots are automatic. Outside allowed roots can request approval when access mode is "ask".
- After a tool result arrives, continue from the result and tell the user what happened.

Local tool rules:
- To use a local tool, emit exactly one or more sigil blocks.
- Every tool block must end with ~@exit@~.
- After tool results come back, continue from those results.
- Never claim a local action happened unless you received its tool result.
- If you are done, answer normally with no tool sigils.

Examples:
User asks: create a folder named Test under E:\\AI_Suite
You reply:
~@mkdir@~ E:\\AI_Suite\\Test ~@exit@~

User asks: show files in Downloads
You reply:
~@explorer@~ C:\\Users\\awoen\\Downloads ~@exit@~

User asks: read a file
You reply:
~@read@~ C:\\Users\\awoen\\Desktop\\notes.txt ~@exit@~

Available tools:
~@read@~ absolute_or_allowed_path ~@exit@~
~@explorer@~ directory_path ~@exit@~
~@file_info@~ file_or_directory_path ~@exit@~
~@write@~ file_path
full file content
~@exit@~
~@append@~ file_path
content to append
~@exit@~
~@replace@~ file_path
---OLD---
exact old text
---NEW---
replacement text
~@exit@~
~@mkdir@~ directory_path ~@exit@~
~@copy@~ source_path
destination_path
~@exit@~
~@move@~ source_path
destination_path
~@exit@~
~@delete@~ path
recursive: false
~@exit@~
~@terminal@~ Windows command ~@exit@~
~@powershell@~ PowerShell command ~@exit@~
~@wsl@~ Linux shell command through WSL ~@exit@~
~@fetch@~ https://example.com ~@exit@~
~@browser_open@~ https://example.com ~@exit@~
~@browser_inspect@~ current or https://example.com ~@exit@~
~@browser_screenshot@~ https://example.com ~@exit@~
~@browser_click@~
url: https://example.com
selector: button.primary
~@exit@~
~@browser_type@~
url: https://example.com
selector: input[name=q]
text: hello
~@exit@~
~@git_status@~ repository_path ~@exit@~
~@git_diff@~ repository_path ~@exit@~
~@git_add@~ repository_path
pathspec
~@exit@~
~@git_commit@~ repository_path
commit message
~@exit@~
~@memory_get@~ key_or_empty_for_all ~@exit@~
~@memory_put@~ key
value
~@exit@~
~@extract_text@~ document_path ~@exit@~
~@window_list@~ check ~@exit@~
~@window_focus@~ window title text or process id ~@exit@~
~@window_close@~ window title text or process id ~@exit@~
~@gui_click@~ x,y ~@exit@~
~@gui_type@~ text to type ~@exit@~
~@gui_hotkey@~ ctrl,shift,s ~@exit@~
~@blender_open@~ blend_file_path_or_empty ~@exit@~
~@blender_python@~ Python script for Blender background mode ~@exit@~
~@excel_info@~ workbook_path ~@exit@~
~@excel_read@~
path: workbook_path
sheet: Sheet1
range: A1:D20
~@exit@~
~@excel_write@~
path: workbook_path
sheet: Sheet1
cell: A1
value: hello
~@exit@~
~@excel_append_row@~
path: workbook_path
sheet: Sheet1
values: one | two | three
~@exit@~
~@mcp_status@~ check ~@exit@~
~@think@~ private planning note to record locally ~@exit@~
""".strip()


TOOL_REMINDER = """GEMINI LOCAL AGENT REMINDER:
You DO have local computer access through sigil tools. Do not say you cannot access files/apps for local-action requests. Call the proper tool and end every tool with ~@exit@~."""
