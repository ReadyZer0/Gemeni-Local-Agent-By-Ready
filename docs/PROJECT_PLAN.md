# Gemini Local Agent Project Plan

## Goal

Gemini Local Agent turns the normal Gemini web chat into a local desktop agent without using the Gemini API, Google Cloud, or extra model-token endpoints. Gemini remains the main reasoning engine through the user's normal Gemini web session, while the desktop app supplies controlled local tools for files, folders, terminals, browsers, Windows UI, Excel, Blender, git, memory, and document extraction.

## Current State

The project is a new Python desktop app built with PySide6 and QtWebEngine. It embeds Gemini in a persistent browser profile, provides a single local composer, watches Gemini's visible replies, extracts local tool calls, runs approved tools through a gateway, and returns results back into the same Gemini chat.

The app currently supports:

- Persistent Gemini browser profile under `data/browser_profile/gemini`.
- One local composer over the Gemini page so users do not have to use the native Gemini chat bar.
- Session modes: `persistent_thread` and `fresh_per_task`.
- Agentic mode selector: `force_tools` or `observe`.
- Gemini mode selector for normal web modes: `fast`, `pro`, and `thinking`.
- Feature buttons for Canvas, Nano Banana/Create Image, Music, and Video.
- Guarded local action approvals for risky actions.
- Access modes: `restricted`, `ask`, and `full`.
- Local JSON/JSONL history, attachment metadata, and browser/session metadata.
- Tool result loop with duplicate-turn protection, queue recovery, stopped-response recovery, and malformed payload guards.

## What Has Been Built So Far

### Desktop Shell

- Created the new project at `E:\AI_Suite\gemini-local-agent`.
- Implemented `app.py` as the PySide6 desktop entrypoint.
- Added a two-pane layout with activity/history on the left and embedded Gemini on the right.
- Added toolbar controls for session mode, approval policy, filesystem access, agentic behavior, and Gemini model/mode.
- Added a unified local composer that visually replaces Gemini's native composer while still sending through the web UI.

### Embedded Gemini Browser

- Implemented `gemini_browser.py` using QtWebEngine.
- Added persistent cookies and storage for normal Gemini login sessions.
- Added DOM automation for sending prompts into Gemini's normal web composer.
- Added attachment upload support using the browser file chooser override.
- Added Canvas/image/music/video activation helpers that try direct controls first and Gemini Tools menu fallback second.
- Added model-mode automation that clicks normal Gemini web controls for Fast, Pro, and Thinking where available.
- Added visible-state extraction that reads Gemini replies, tool sigils, busy/stopped state, URL, title, and Canvas activity.

### Agent Loop

- Implemented `agent_loop.py` as the Gemini-first loop.
- Sends clean user prompts to Gemini instead of wrapping every user request in large tool instructions.
- Parses Gemini replies for sigil tool calls.
- Executes local tools and sends tool results back into the same Gemini thread.
- Queues new user tasks while a previous turn is still running.
- Recovers when Gemini is idle but the app still thinks a turn is running.
- Detects Gemini "stopped response" and unlocks the agent.
- Added force-tools enforcement for local-action requests.
- Added guardrails for placeholder tools such as `tool`, `tool_name`, `local_tool`, and unknown/disabled tools.
- Added mismatched-tool detection so read-only shell listing commands do not satisfy create-file requests.
- Added malformed write/append/replace payload detection so instruction text is not written as a file path or file body.
- Added artifact recovery for Gemini-created TXT cards and code blocks.
- Added synthetic HTML recovery: if Gemini answers a website request with prose instead of code, the app can generate a usable HTML page from that brief.

### Tool Protocol

- Implemented `tool_protocol.py`.
- Supports normalized sigil blocks such as `~@read@~`, `~@write@~`, `~@powershell@~`, and `~@excel_read@~`.
- Handles aliases such as `read_file`, `cat`, `mkdri`, `ls`, and `ps`.
- Builds tool result messages for returning execution output to Gemini.
- Renders the Gemini-facing tool contract.

### Local Tool Gateway

- Implemented `mcp_gateway.py`.
- Added file and folder tools: `read`, `explorer`, `file_info`, `write`, `append`, `replace`, `mkdir`, `copy`, `move`, and `delete`.
- Added shell tools: `terminal`, `powershell`, and `wsl`.
- Added browser helpers: `fetch`, `browser_open`, `browser_inspect`, `browser_screenshot`, `browser_click`, and `browser_type`.
- Added git helpers: `git_status`, `git_diff`, `git_add`, and `git_commit`.
- Added memory and thinking tools: `memory_get`, `memory_put`, and `think`.
- Added Windows GUI tools: `window_list`, `window_focus`, `window_close`, `gui_click`, `gui_type`, and `gui_hotkey`.
- Added Blender helpers: `blender_open` and `blender_python`.
- Added Excel helpers: `excel_info`, `excel_read`, `excel_write`, and `excel_append_row`.
- Added document extraction fallback with MarkItDown when available.
- Added MCP status reporting.

### Safety And Approvals

- Implemented `approvals.py` for approval dialogs.
- Auto-approves low-risk reads, directory listings, fetches, memory, git status/diff, Excel reads, and window listing.
- Requires approval for writes, deletes, shell commands, PowerShell, WSL, GUI actions, Blender actions, Excel writes, git mutations, and browser click/type/screenshot.
- Supports `guarded`, `mostly_auto`, and `full_auto` approval policies.
- Supports `restricted`, `ask`, and `full` filesystem access modes.

### History And Attachments

- Implemented `history_store.py` for local session metadata and JSONL event history.
- Implemented `attachments.py` for copying attached files/images into local attachment storage.
- Added clipboard image paste flow.
- Added local storage for Canvas/thread metadata and active Gemini URL.

### Tests And Verification

- Added unit tests for:
  - Tool protocol parsing and aliases.
  - Gemini send/read-state scripts.
  - Local gateway file/memory/access behavior.
  - Artifact recovery.
  - Placeholder/invalid tool detection.
  - Mismatched and malformed tool calls.
  - Synthetic HTML recovery.
  - Model-mode automation script generation.
- Current validation commands:
  - `python -m py_compile app.py agent_loop.py approvals.py attachments.py gemini_browser.py history_store.py mcp_gateway.py tool_protocol.py`
  - `python -m unittest discover -s tests`
  - `python app.py --smoke`

## Current Known Limitations

- Gemini web UI is not a real MCP client, so the app uses a normalized sigil protocol and maps those calls to native/MCP-like tools locally.
- Gemini can still sometimes answer with prose or Gemini artifacts; the app now recovers many common file/website cases but this remains an ongoing hardening area.
- Canvas support currently means reliable visibility, session continuity, feature activation, and metadata tracking. Full automated Canvas DOM editing is intentionally deferred.
- Model switching depends on Gemini web UI labels and may require small selector updates if Google changes the interface.
- GitHub is disabled by default in the tool gateway for safety.
- The app cannot create or log into a Google account. The user logs into Gemini manually once in the embedded browser.

## Roadmap

### Phase 1: Publishable Baseline

- Keep the current Python/PySide6 desktop app stable.
- Keep all local browser profile, sessions, attachments, logs, memory, and artifacts out of git.
- Add complete project documentation and publish the repository.
- Keep tests green before each push.

### Phase 2: Stronger Agent Reliability

- Add a structured local planner that can pre-select the likely tool for simple requests before Gemini responds.
- Add stronger result validation: confirm written files exist and contain non-placeholder content before returning success.
- Add automatic retry paths for common Gemini mistakes without sending confusing examples.
- Add a local "Run Visible Reply" recovery flow that can extract code/prose from the current page and save artifacts.

### Phase 3: Better Browser And App Control

- Improve Playwright integration for richer browser inspection, screenshots, and DOM actions.
- Add optional Chrome DevTools Protocol connection to an existing local Chrome/Edge profile.
- Add safer app/window targeting with process/title previews before GUI control.
- Add reusable GUI macros for common Windows workflows.

### Phase 4: Canvas And Creative Modes

- Track Canvas artifacts more explicitly in local history.
- Add better Canvas creation/open detection.
- Add optional export/download detection for Canvas outputs.
- Keep full Canvas DOM editing as an experimental feature only after stable selectors are identified.

### Phase 5: MCP Server Integration Layer

- Keep the Gemini-facing protocol simple.
- Add a backend adapter layer that can route calls to real MCP servers where installed.
- Add health checks for filesystem, memory, sequential-thinking, Playwright, Chrome DevTools, git, fetch, MarkItDown, desktop commander, Blender, and Excel capabilities.
- Add a UI showing which MCP/native tools are available, missing, or disabled.

### Phase 6: Packaging

- Add a launcher script for Windows.
- Add optional PyInstaller packaging.
- Add first-run checks for Python, PySide6, QtWebEngine, Playwright, openpyxl, pyautogui, Blender, WSL, and MarkItDown.
- Add a settings UI for paths, access mode, approval policy, output folder, and enabled tools.

## Security Notes

- Do not commit `data/browser_profile`, `data/sessions`, `data/attachments`, `data/logs`, `data/memory`, or generated artifacts.
- Keep approval mode on `guarded` while testing system control.
- Use `restricted` or `ask` access mode for normal use.
- Use `full` only when you understand that Gemini can resolve arbitrary local paths through approved tools.
- Review every approval dialog carefully before allowing terminal, PowerShell, WSL, GUI, Blender, Excel write, delete, move, copy, or git mutation actions.
