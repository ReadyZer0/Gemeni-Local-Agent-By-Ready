# Gemini Local Agent

Gemini Local Agent is a fresh desktop project that uses the normal logged-in
Gemini web chat as the primary agent brain. It does not call the Gemini API,
does not require Google Cloud, and does not need a local LLM for the main loop.

The desktop app provides:

- an embedded persistent Gemini browser session
- a Gemini-first tool loop using sigil tool calls
- guarded local actions for files, folders, shell commands, PowerShell, WSL, git, fetch, memory, browser helpers, app windows, GUI automation, Blender, and Excel workbooks
- image/file attachment handoff into the Gemini composer
- Canvas-aware session tracking
- local JSON/JSONL history

## Quick Start

```powershell
cd E:\AI_Suite\gemini-local-agent
python -m pip install -r requirements.txt
python app.py
```

Log in to Gemini in the right-side browser pane. Then use the single
`Ask Gemini...` composer attached to the Gemini pane. The left panel is only
for activity, transcript, and tool logs. Gemini can request local actions using
the tool protocol shown in `tool_protocol.py`; the app executes those tools and
sends results back into the same Gemini chat.

By default, the app visually hides Gemini's native web composer so the local
tool-aware composer is the only bar you need to use. The hidden Gemini composer
still exists underneath for browser automation. Set `hide_native_composer` to
`false` in `config.json` if you want the original Gemini bar visible again.

The unified composer includes quick Gemini feature buttons for `Canvas`,
`Nano Banana`, `Music`, and `Video`. Each button first tries to activate the
matching Gemini web UI control, including items hidden behind the Gemini Tools
menu. If Gemini changes the page structure and the click is not found, the next
message is still sent with a short instruction asking Gemini to use that mode.

The app cannot create or enter a Google account for you. Use the embedded
Gemini pane to log in once; the persistent browser profile keeps that session
under `data/browser_profile/gemini`.

Tool instructions are sent automatically at the start of each new local Gemini
session. If that automatic seed cannot be sent because Gemini is logged out or
the composer is unavailable, the app sends the tool instructions as a separate
preflight message before the first user request. Normal user prompts are sent
cleanly without the local-agent reminder wrapper.

## Safety Model

The default approval policy is `guarded`.

The default agentic mode is `force_tools`. When Gemini answers a local/system
control request with prose or a Gemini artifact instead of a sigil tool call,
the app automatically sends a compact enforcement message requiring tool blocks
only. Switch `Agentic` to `observe` in the toolbar if you want Gemini to behave
like normal chat without this correction layer.

Auto-approved tools include reads, directory listing, fetch, browser inspection,
memory, Excel reads, window listing, and git status/diff. Writes, replacements,
append/delete/copy/move, terminal commands, PowerShell, WSL, screenshots,
browser click/type, GUI automation, Blender actions, Excel writes, git add, and
git commit require an approval dialog.

Filesystem access modes live in `config.json`. The default is `ask`, so allowed
roots are automatic and outside-root paths can request approval:

- `restricted`: only paths under configured roots are available.
- `ask`: outside-root paths trigger a path access request.
- `full`: Gemini tools can resolve any local path, while risky actions still follow the approval policy.

Default allowed roots are `E:\AI_Suite`, Desktop, Documents, Downloads, and the
project folder.

## Tool Families

- Files and folders: read, list, info, write, replace, append, mkdir, copy, move, delete.
- Shells: Windows terminal commands, PowerShell, and WSL/Linux commands.
- Browser: fetch/inspect, screenshot, click, and type through Playwright when installed.
- Windows apps: list/focus/close windows and use guarded GUI click/type/hotkey through pyautogui.
- Blender: open Blender files or run approved background Python scripts.
- Excel: inspect workbooks, read ranges, write cells, and append rows through openpyxl.
- Memory and planning: local key-value memory plus thinking notes.

## Session Modes

- `persistent_thread`: keep one Gemini web thread linked to the current local session.
- `fresh_per_task`: ask Gemini to start a new chat before each task.

Canvas support in v1 keeps Gemini visible, records Canvas-active state, and
preserves thread continuity. It intentionally does not attempt fragile Canvas DOM
editing.

## Verification

```powershell
python -m py_compile app.py agent_loop.py approvals.py attachments.py gemini_browser.py history_store.py mcp_gateway.py tool_protocol.py
python -m unittest discover -s tests
python app.py --smoke
```

## Project Plan And Progress

The full implementation plan, completed work, known limitations, roadmap, and
security notes are documented in [docs/PROJECT_PLAN.md](docs/PROJECT_PLAN.md).
