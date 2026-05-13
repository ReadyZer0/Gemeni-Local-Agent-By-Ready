from __future__ import annotations

import hashlib
import html
import re
import threading
import uuid
from pathlib import Path

from PySide6.QtCore import QObject, QTimer, Signal, Slot

from attachments import Attachment, AttachmentManager
from gemini_browser import GeminiBrowser
from history_store import HistoryStore
from mcp_gateway import GatewayResult, ToolGateway
from tool_protocol import ToolCall, build_tool_result_message, first_line_and_body, parse_reply, render_tool_contract


class GeminiAgentLoop(QObject):
    PLACEHOLDER_TOOL_NAMES = {"tool", "tool_name", "local_tool", "actual_tool", "your_tool"}

    log_signal = Signal(str)
    status_signal = Signal(str)
    final_signal = Signal(str)
    tool_result_signal = Signal(str)
    _tool_results_ready = Signal(str, object)

    def __init__(
        self,
        config: dict,
        browser: GeminiBrowser,
        gateway: ToolGateway,
        history: HistoryStore,
        attachments: AttachmentManager,
        parent=None,
    ):
        super().__init__(parent)
        self.config = config
        self.browser = browser
        self.gateway = gateway
        self.history = history
        self.attachments = attachments
        self.running = False
        self.current_turn_id = ""
        self.current_loop_count = 0
        self.last_reply_signature = ""
        self.stable_count = 0
        self.processed_reply_signatures: set[str] = set()
        self.call_counts: dict[str, int] = {}
        self.current_user_text = ""
        self.queued_task: tuple[str, list[str]] | None = None
        self.repeated_processed_reply_count = 0
        self.tool_enforcement_count = 0
        self._tool_results_ready.connect(self._send_tool_results)

    def start_task(self, user_text: str, file_paths: list[str] | None = None) -> None:
        if self.running:
            self.queued_task = (user_text, list(file_paths or []))
            self.log_signal.emit("[QUEUE] Gemini agent is still finishing the previous turn; queued your next message.")
            self.browser.read_state(self._recover_or_wait_for_queue)
            return
        self._start_task_now(user_text, file_paths)

    def _start_task_now(self, user_text: str, file_paths: list[str] | None = None) -> None:
        session_mode = self.config.get("gemini", {}).get("session_mode", "persistent_thread")
        session_id = self.history.ensure_session(session_mode=session_mode)
        stored_attachments = self.attachments.add_files(file_paths or [], session_id)
        self.history.add_event("user", {"text": user_text, "attachments": [item.to_dict() for item in stored_attachments]})
        self.running = True
        self.current_turn_id = uuid.uuid4().hex[:10]
        self.current_loop_count = 0
        self.current_user_text = user_text
        self.last_reply_signature = ""
        self.stable_count = 0
        self.processed_reply_signatures.clear()
        self.call_counts.clear()
        self.repeated_processed_reply_count = 0
        self.tool_enforcement_count = 0
        self.status_signal.emit("Sending to Gemini")

        def send_user_request():
            prompt = self._build_user_prompt(user_text, stored_attachments)
            upload_paths = [item.stored_path for item in stored_attachments]
            self.browser.send_prompt(prompt, upload_paths, callback=lambda result: self._after_send(result))

        def prepare_then_send():
            self._seed_tools_before_user_request(send_user_request)

        if session_mode == "fresh_per_task":
            self.log_signal.emit("[SESSION] Starting a fresh Gemini chat for this task.")
            self.browser.new_chat(lambda _result: QTimer.singleShot(1200, prepare_then_send))
        else:
            prepare_then_send()

    def stop(self) -> None:
        self.running = False
        self.queued_task = None
        self.status_signal.emit("Stopped")
        self.log_signal.emit("[USER] Stop requested.")

    def process_visible_state(self, state: dict, user_hint: str = "manual visible Gemini reply") -> None:
        reply = str((state or {}).get("latestReply") or (state or {}).get("bodyText") or "").strip()
        if not reply:
            self.log_signal.emit("[VISIBLE] No readable Gemini reply found on the page.")
            return
        if self.running:
            self.log_signal.emit("[VISIBLE] Agent was already running; processing the visible reply anyway.")
        self.running = True
        self.current_turn_id = uuid.uuid4().hex[:10]
        self.current_loop_count = 0
        self.current_user_text = user_hint
        self.last_reply_signature = ""
        self.stable_count = 0
        self.call_counts.clear()
        self.repeated_processed_reply_count = 0
        self.tool_enforcement_count = 0
        self.status_signal.emit("Processing visible Gemini tools")
        self._process_reply(reply)

    def _build_user_prompt(self, user_text: str, attachments: list[Attachment]) -> str:
        base_prompt = f"{user_text.strip()}{AttachmentManager.prompt_summary(attachments)}".strip()
        hint = GeminiAgentLoop._recommended_tool_hint(user_text)
        if hint:
            return f"{base_prompt}\n\n[Planner Hint: {hint}]"
        return base_prompt

    def _should_seed_tools(self) -> bool:
        meta = self.history.metadata()
        seed_once = bool(self.config.get("agent", {}).get("tool_contract_once_per_session", True))
        return (not seed_once) or (not meta.get("tool_contract_seeded"))

    def _seed_tools_before_user_request(self, send_user_request) -> None:
        meta = self.history.metadata()
        if meta.get("tool_contract_seed_in_progress"):
            self.log_signal.emit("[SEED] Waiting for the current tool-instruction seed to finish before sending your request.")
            delay = int(self.config.get("gemini", {}).get("tool_seed_user_delay_ms", 1600))
            QTimer.singleShot(delay, lambda: self._seed_tools_before_user_request(send_user_request))
            return
        if not self._should_seed_tools():
            send_user_request()
            return
        self.status_signal.emit("Teaching Gemini local tools")
        self.log_signal.emit("[SEED] Sending Gemini Local Agent instructions separately before the user request.")
        contract = (
            self.tool_contract()
            + "\n\nAcknowledge briefly that you can use these tools. Then wait for the next user message."
        )
        self.history.update_metadata(tool_contract_seed_in_progress=True)

        def handle_seed_result(result):
            ok = bool(isinstance(result, dict) and result.get("ok"))
            self.history.update_metadata(tool_contract_seed_in_progress=False)
            if ok:
                self.history.update_metadata(tool_contract_seeded=True)
                self.log_signal.emit("[SEED] Tool instructions sent separately; sending the user request next.")
                delay = int(self.config.get("gemini", {}).get("tool_seed_user_delay_ms", 1600))
                QTimer.singleShot(delay, send_user_request)
            else:
                self.log_signal.emit(f"[SEED] Could not send tool instructions first: {result}")
                self.log_signal.emit("[SEED] Sending the clean user request anyway; refusal correction can teach tools if needed.")
                send_user_request()

        self.browser.send_prompt(contract, [], callback=handle_seed_result)

    def tool_contract(self) -> str:
        return render_tool_contract(
            access_mode=str(self.config.get("security", {}).get("access_mode", "restricted")),
            approval_policy=str(self.config.get("app", {}).get("approval_policy", "guarded")),
            roots=self.config.get("security", {}).get("filesystem_roots", []),
            enabled_tools=self.config.get("tools", {}).get("enabled", []),
        )

    def _after_send(self, result: dict) -> None:
        if not self.running:
            return
        if not result or not result.get("ok"):
            self.log_signal.emit(f"[WARN] Gemini send may need manual help: {result}")
            self.status_signal.emit("Manual send may be needed")
        else:
            self.log_signal.emit(f"[GEMINI] Prompt sent by {result.get('method', 'browser automation')}.")
            self.status_signal.emit("Waiting for Gemini")
        self._schedule_poll()

    def _schedule_poll(self) -> None:
        if not self.running:
            return
        interval = int(self.config.get("gemini", {}).get("poll_interval_ms", 1500))
        QTimer.singleShot(interval, lambda: self.browser.read_state(self._handle_browser_state))

    def _handle_browser_state(self, state: dict) -> None:
        if not self.running:
            return
        url = str(state.get("url") or "")
        canvas_active = bool(state.get("canvasActive"))
        self.history.update_metadata(gemini_url=url, canvas_active=canvas_active)
        if canvas_active:
            self.log_signal.emit("[CANVAS] Gemini Canvas appears active for this session.")
        if state.get("busy"):
            self.status_signal.emit("Gemini is responding")
            self._schedule_poll()
            return
        if state.get("stopped"):
            self.history.add_event("gemini", {"text": "Gemini response was stopped in the web UI.", "has_tools": False})
            self.log_signal.emit("[GEMINI] Gemini reports the response was stopped; local agent unlocked.")
            self._finish_turn("Ready")
            return
        reply = str(state.get("latestReply") or "").strip()
        if not reply:
            self._schedule_poll()
            return
        signature = hashlib.sha256(reply.encode("utf-8", errors="replace")).hexdigest()
        if signature == self.last_reply_signature:
            self.stable_count += 1
        else:
            self.last_reply_signature = signature
            self.stable_count = 1
        stable_required = int(self.config.get("gemini", {}).get("reply_stable_polls", 2))
        if self.stable_count < stable_required:
            self._schedule_poll()
            return
        if signature in self.processed_reply_signatures:
            self.repeated_processed_reply_count += 1
            duplicate_limit = int(self.config.get("gemini", {}).get("processed_reply_unlock_polls", 2))
            if self.repeated_processed_reply_count >= duplicate_limit:
                self.log_signal.emit("[RECOVER] Gemini reply was already processed and the page is idle; unlocking the agent.")
                self._finish_turn("Ready")
            else:
                self._schedule_poll()
            return
        self.repeated_processed_reply_count = 0
        self.processed_reply_signatures.add(signature)
        self._process_reply(reply)

    def _recover_or_wait_for_queue(self, state: dict) -> None:
        if not self.running:
            self._start_queued_task_if_any()
            return
        if state.get("busy"):
            self.log_signal.emit("[QUEUE] Gemini still appears busy; queued message will wait.")
            return
        reply = str((state or {}).get("latestReply") or "").strip()
        if reply:
            self.log_signal.emit("[RECOVER] Processing the visible completed Gemini reply before sending the queued message.")
            self._handle_browser_state(state)
            return
        self.log_signal.emit("[RECOVER] No active Gemini response found; unlocking and sending the queued message.")
        self._finish_turn("Ready")

    def _finish_turn(self, status: str = "Ready") -> None:
        self.running = False
        self.status_signal.emit(status)
        self._start_queued_task_if_any()

    def _start_queued_task_if_any(self) -> None:
        if self.running or not self.queued_task:
            return
        user_text, file_paths = self.queued_task
        self.queued_task = None
        QTimer.singleShot(100, lambda: self._start_task_now(user_text, file_paths))

    def _process_reply(self, reply: str) -> None:
        parsed = parse_reply(reply)
        self.history.add_event("gemini", {"text": parsed.chat_text or reply, "has_tools": parsed.has_tools})
        if not parsed.has_tools:
            if self._looks_like_plain_gemini_refusal(reply):
                self._correct_tool_refusal(reply)
                return
            artifact_result = self._maybe_save_local_artifact(reply)
            if artifact_result:
                final = parsed.chat_text or reply
                final = f"{final}\n\nLOCAL ARTIFACT:\n{artifact_result}"
                self.final_signal.emit(final)
                self._finish_turn("Ready")
                return
            if self._should_enforce_tool_call(reply):
                self._enforce_tool_call(reply)
                return
            final = parsed.chat_text or reply
            self.final_signal.emit(final)
            self._finish_turn("Ready")
            return
        invalid_calls = self._invalid_tool_calls(parsed.tool_calls)
        if invalid_calls:
            self._correct_invalid_tool_call(invalid_calls)
            return
        mismatched_calls = self._mismatched_tool_calls(parsed.tool_calls)
        if mismatched_calls:
            self._correct_mismatched_tool_call(mismatched_calls)
            return
        malformed_calls = self._malformed_payload_tool_calls(parsed.tool_calls)
        if malformed_calls:
            self._correct_malformed_payload_call(malformed_calls)
            return
        max_loops = int(self.config.get("agent", {}).get("max_tool_loops", 12))
        self.current_loop_count += 1
        if self.current_loop_count > max_loops:
            message = f"[STOPPED] Max tool loops reached ({max_loops})."
            self.final_signal.emit(message)
            self._finish_turn("Tool loop stopped")
            return
        self.status_signal.emit("Running local tools")
        self.log_signal.emit(f"[TOOLS] Gemini requested {len(parsed.tool_calls)} local tool(s).")
        threading.Thread(target=self._execute_tools_worker, args=(parsed.tool_calls,), daemon=True).start()

    def _looks_like_plain_gemini_refusal(self, reply: str) -> bool:
        text = str(reply or "").lower()
        refusal_markers = [
            "i don't have direct access",
            "i do not have direct access",
            "i can't access your computer",
            "i cannot access your computer",
            "i can't create",
            "i cannot create",
            "as an ai",
            "press ctrl",
            "right-click",
            "new folder",
        ]
        local_action_words = [
            "file",
            "folder",
            "directory",
            "computer",
            "powershell",
            "excel",
            "browser",
            "window",
            "blender",
            "create",
            "delete",
            "edit",
            "append",
        ]
        user = str(self.current_user_text or "").lower()
        return any(marker in text for marker in refusal_markers) and any(word in user for word in local_action_words)

    def _should_enforce_tool_call(self, reply: str) -> bool:
        if not self.config.get("agent", {}).get("force_tool_calls_for_local_actions", True):
            return False
        if self.tool_enforcement_count >= int(self.config.get("agent", {}).get("max_tool_enforcement_retries", 2)):
            return False
        if not self._requires_local_tool(self.current_user_text):
            return False
        text = str(reply or "").lower()
        if "tool results for turn" in text:
            return False
        return True

    @staticmethod
    def _requires_local_tool(user_text: str) -> bool:
        text = str(user_text or "").lower()
        action_words = [
            "create",
            "make",
            "write",
            "save",
            "edit",
            "append",
            "replace",
            "delete",
            "remove",
            "copy",
            "move",
            "rename",
            "read",
            "open",
            "list",
            "show",
            "inspect",
            "run",
            "execute",
            "click",
            "type",
            "focus",
            "close",
            "control",
            "install",
            "download",
            "fetch",
        ]
        target_words = [
            "file",
            "folder",
            "directory",
            "desktop",
            "documents",
            "downloads",
            ".txt",
            ".html",
            ".css",
            ".js",
            ".py",
            "website",
            "webpage",
            "browser",
            "window",
            "app",
            "application",
            "terminal",
            "powershell",
            "cmd",
            "wsl",
            "linux",
            "excel",
            "workbook",
            "spreadsheet",
            "blender",
            "git",
            "repo",
            "repository",
            "url",
            "web",
            "page",
            "screenshot",
        ]
        path_pattern = re.search(r"(?:[A-Za-z]:\\|\\\\|/[\w.-])", text)
        return (any(word in text for word in action_words) and any(word in text for word in target_words)) or bool(path_pattern)

    def _enforce_tool_call(self, previous_reply: str) -> None:
        self.tool_enforcement_count += 1
        self.current_loop_count += 1
        max_loops = int(self.config.get("agent", {}).get("max_tool_loops", 12))
        if self.current_loop_count > max_loops:
            self.log_signal.emit("[ENFORCE] Max loop count reached while enforcing tool use.")
            self.final_signal.emit(previous_reply)
            self._finish_turn("Ready")
            return
        self.log_signal.emit("[ENFORCE] Gemini answered without local tools; requiring a sigil tool call.")
        correction = self._tool_enforcement_prompt()
        self.status_signal.emit("Forcing Gemini tool mode")
        self.browser.send_prompt(correction, [], callback=lambda result: self._after_send(result))

    def _tool_enforcement_prompt(self) -> str:
        default_root = self.config.get("security", {}).get("terminal_cwd") or "E:\\AI_Suite"
        allowed_tools = ", ".join(self._agentic_tool_names())
        recommendation = self._recommended_tool_hint(self.current_user_text)
        return (
            "LOCAL AGENT ENFORCEMENT:\n"
            "The previous response did not use the required local tool call. For this request, answer ONLY with one or more sigil tool blocks. No prose, no Gemini artifact cards, no manual instructions.\n\n"
            "Use only a REAL tool name from this list:\n"
            f"{allowed_tools}\n\n"
            f"{recommendation}\n\n"
            "Do not invent tool names or use generic placeholders.\n\n"
            f"{self._tool_examples_text(default_root)}\n\n"
            f"If the user did not provide a destination path, choose a sensible path under {default_root} or the user's Desktop.\n\n"
            f"USER REQUEST:\n{self.current_user_text.strip()}"
        )

    def _enabled_tool_names(self) -> set[str]:
        return {
            str(name).strip().lower()
            for name in self.config.get("tools", {}).get("enabled", [])
            if str(name).strip()
        }

    def _agentic_tool_names(self) -> list[str]:
        preferred = [
            "write",
            "append",
            "replace",
            "mkdir",
            "delete",
            "copy",
            "move",
            "read",
            "explorer",
            "file_info",
            "powershell",
            "terminal",
            "wsl",
            "fetch",
            "browser_open",
            "browser_inspect",
            "browser_screenshot",
            "browser_click",
            "browser_type",
            "window_list",
            "window_focus",
            "window_close",
            "gui_click",
            "gui_type",
            "gui_hotkey",
            "excel_info",
            "excel_read",
            "excel_write",
            "excel_append_row",
            "blender_open",
            "blender_python",
            "git_status",
            "git_diff",
            "git_add",
            "git_commit",
            "memory_get",
            "memory_put",
            "extract_text",
            "mcp_status",
            "think",
        ]
        enabled = self._enabled_tool_names()
        names = [name for name in preferred if not enabled or name in enabled]
        extras = sorted(name for name in enabled if name not in preferred)
        return names + extras

    @staticmethod
    def _tool_examples_text(default_root: str) -> str:
        desktop = "C:\\Users\\awoen\\Desktop"
        return (
            "Correct examples:\n"
            "Create/write a file:\n"
            "~@write@~\n"
            f"{desktop}\\example.txt\n"
            "file content here\n"
            "~@exit@~\n\n"
            "Create a folder:\n"
            "~@mkdir@~\n"
            f"{desktop}\\hello gemini\n"
            "~@exit@~\n\n"
            "List files/folders:\n"
            "~@explorer@~\n"
            f"{desktop}\n"
            "~@exit@~\n\n"
            "Run PowerShell:\n"
            "~@powershell@~\n"
            f"Get-ChildItem {default_root}\n"
            "~@exit@~"
        )

    def _invalid_tool_calls(self, calls: list[ToolCall]) -> list[ToolCall]:
        enabled = self._enabled_tool_names()
        invalid: list[ToolCall] = []
        for call in calls:
            if call.name in self.PLACEHOLDER_TOOL_NAMES:
                invalid.append(call)
            elif enabled and call.name not in enabled:
                invalid.append(call)
        return invalid

    def _mismatched_tool_calls(self, calls: list[ToolCall]) -> list[ToolCall]:
        if not self._looks_like_create_file_request(self.current_user_text):
            return []
        mismatched: list[ToolCall] = []
        for call in calls:
            if call.name in {"powershell", "terminal", "wsl"} and self._is_read_only_shell_listing(call.raw):
                mismatched.append(call)
        return mismatched

    def _malformed_payload_tool_calls(self, calls: list[ToolCall]) -> list[ToolCall]:
        malformed: list[ToolCall] = []
        for call in calls:
            if call.name in {"write", "append", "replace"}:
                path_text, body = first_line_and_body(call.raw)
                if not self._is_plausible_file_path(path_text) or self._looks_like_placeholder_file_body(body):
                    malformed.append(call)
        return malformed

    @staticmethod
    def _is_plausible_file_path(path_text: str) -> bool:
        value = str(path_text or "").strip().strip('"').strip("'")
        lowered = value.lower()
        if not value:
            return False
        bad_fragments = [
            "~@",
            "tool block",
            "no prose",
            "gemini artifact card",
            "required format",
            "requested file content here",
            "complete file content",
        ]
        if any(fragment in lowered for fragment in bad_fragments):
            return False
        if re.search(r"[\r\n<>|?*]", value):
            return False
        path = Path(value)
        known_suffixes = {
            ".txt",
            ".md",
            ".html",
            ".htm",
            ".css",
            ".js",
            ".mjs",
            ".py",
            ".json",
            ".csv",
            ".xml",
            ".svg",
            ".xlsx",
            ".xls",
            ".docx",
            ".pptx",
            ".pdf",
            ".blend",
        }
        known_no_ext = {"dockerfile", "makefile", "readme", "license", "gemfile", "procfile"}
        if path.suffix.lower() in known_suffixes:
            return True
        if path.name.lower() in known_no_ext:
            return True
        has_separator = "\\" in value or "/" in value
        is_absolute = bool(re.match(r"^(?:[A-Za-z]:\\|\\\\|/)", value))
        return bool((has_separator or is_absolute) and path.name and "." not in path.name)

    @staticmethod
    def _looks_like_placeholder_file_body(body: str) -> bool:
        text = str(body or "").strip().lower()
        if not text:
            return False
        placeholders = [
            "requested file content here",
            "complete requested file content",
            "generate the complete requested file content",
            "remaining payload lines",
            "complete file content",
            "replace this line",
            "content goes here",
            "file content here",
        ]
        return any(item in text for item in placeholders)

    @classmethod
    def _looks_like_create_file_request(cls, user_text: str) -> bool:
        return cls._looks_like_artifact_request(user_text) or cls._looks_like_plain_file_request(user_text)

    @staticmethod
    def _is_read_only_shell_listing(raw: str) -> bool:
        command = str(raw or "").strip().lower()
        if not command:
            return False
        mutating_markers = [
            "set-content",
            "add-content",
            "out-file",
            "new-item",
            "mkdir",
            "md ",
            "copy-item",
            "move-item",
            "remove-item",
            "del ",
            "rm ",
            ">",
            "tee",
        ]
        if any(marker in command for marker in mutating_markers):
            return False
        return bool(re.search(r"(^|[;&|\s])(get-childitem|gci|dir|ls)(\s|$)", command))

    @staticmethod
    def _recommended_tool_hint(user_text: str) -> str:
        text = str(user_text or "").lower()
        hint = ""
        if GeminiAgentLoop._looks_like_plain_file_request(text):
            hint = (
                "Recommended first tool for this request: use ~@write@~. "
                "Put the full target file path on the first payload line, then the requested text content on the remaining lines."
            )
        elif GeminiAgentLoop._looks_like_artifact_request(text):
            hint = (
                "Recommended first tool for this request: use ~@write@~. "
                "Create the requested local file directly instead of making a Gemini artifact card."
            )
        elif "folder" in text or "directory" in text:
            hint = "Recommended first tool for this request: use ~@mkdir@~ for creation or ~@explorer@~ for listing."
        elif any(word in text for word in ["read", "open file", "show file"]):
            hint = "Recommended first tool for this request: use ~@read@~ for files or ~@explorer@~ for folders."
        elif any(word in text for word in ["run", "powershell", "command", "terminal", "wsl"]):
            hint = "Recommended first tool for this request: use ~@powershell@~, ~@terminal@~, or ~@wsl@~ as requested."
        else:
            hint = "Choose the single real tool that directly performs the user's local action."

        if "run it" in text:
            hint += " Use the FULL ABSOLUTE PATH of the file you intend to run."

        return hint.strip()

    def _correct_invalid_tool_call(self, invalid_calls: list[ToolCall]) -> None:
        names = ", ".join(sorted({call.name for call in invalid_calls}))
        max_retries = int(self.config.get("agent", {}).get("max_tool_enforcement_retries", 2))
        if self.tool_enforcement_count >= max_retries:
            message = f"[STOPPED] Gemini used invalid or disabled local tool name(s): {names}. No local action was run."
            self.log_signal.emit(f"[BLOCKED] {message}")
            self.final_signal.emit(message)
            self._finish_turn("Tool name blocked")
            return
        self.tool_enforcement_count += 1
        self.current_loop_count += 1
        max_loops = int(self.config.get("agent", {}).get("max_tool_loops", 12))
        if self.current_loop_count > max_loops:
            message = f"[STOPPED] Max tool loops reached while correcting invalid tool name(s): {names}."
            self.log_signal.emit(f"[BLOCKED] {message}")
            self.final_signal.emit(message)
            self._finish_turn("Tool loop stopped")
            return
        default_root = self.config.get("security", {}).get("terminal_cwd") or "E:\\AI_Suite"
        allowed_tools = ", ".join(self._agentic_tool_names())
        self.log_signal.emit(f"[CORRECT] Gemini used invalid/disabled tool name(s): {names}; requesting a real tool name.")
        correction = (
            "LOCAL AGENT TOOL NAME ERROR:\n"
            f"You used invalid or disabled tool name(s): {names}.\n"
            "Do not use placeholders. The exact block marker must contain a real enabled tool name.\n"
            "Reply again with ONLY corrected tool blocks. No prose, no explanation, no Gemini artifact card.\n\n"
            "Allowed real tool names:\n"
            f"{allowed_tools}\n\n"
            "The placeholder tool name is invalid. Use ~@write@~, ~@mkdir@~, ~@read@~, ~@explorer@~, ~@powershell@~, or another real enabled tool instead.\n\n"
            f"{self._tool_examples_text(default_root)}\n\n"
            f"USER REQUEST:\n{self.current_user_text.strip()}"
        )
        self.status_signal.emit("Correcting Gemini tool name")
        self.browser.send_prompt(correction, [], callback=lambda result: self._after_send(result))

    def _correct_mismatched_tool_call(self, mismatched_calls: list[ToolCall]) -> None:
        names = ", ".join(sorted({call.name for call in mismatched_calls}))
        max_retries = int(self.config.get("agent", {}).get("max_tool_enforcement_retries", 2))
        if self.tool_enforcement_count >= max_retries:
            message = f"[STOPPED] Gemini chose tool(s) that did not perform the requested file creation: {names}. No local action was run."
            self.log_signal.emit(f"[BLOCKED] {message}")
            self.final_signal.emit(message)
            self._finish_turn("Tool mismatch blocked")
            return
        self.tool_enforcement_count += 1
        self.current_loop_count += 1
        self.log_signal.emit(f"[CORRECT] Gemini chose {names} for a create-file request; requiring a write tool instead.")
        target_path = self._suggested_output_path()
        correction = (
            "LOCAL AGENT TOOL MISMATCH:\n"
            "The previous tool call only inspected/listed files and did not create the requested file.\n"
            "For this create-file request, generate the requested local file with exactly one write block and no text outside the block.\n\n"
            f"Use this exact target path on the first payload line: {target_path}\n"
            "For a website/webpage, the file content must start with <!DOCTYPE html> and end with </html>.\n"
            "Do not copy any instruction sentence into the file content.\n\n"
            f"USER REQUEST:\n{self.current_user_text.strip()}"
        )
        self.status_signal.emit("Correcting Gemini tool choice")
        self.browser.send_prompt(correction, [], callback=lambda result: self._after_send(result))

    def _correct_malformed_payload_call(self, malformed_calls: list[ToolCall]) -> None:
        names = ", ".join(f"{call.name}:{call.index}" for call in malformed_calls)
        max_retries = int(self.config.get("agent", {}).get("max_tool_enforcement_retries", 2))
        if self.tool_enforcement_count >= max_retries:
            message = f"[STOPPED] Gemini produced malformed tool payload(s): {names}. No local action was run."
            self.log_signal.emit(f"[BLOCKED] {message}")
            self.final_signal.emit(message)
            self._finish_turn("Tool payload blocked")
            return
        self.tool_enforcement_count += 1
        self.current_loop_count += 1
        target_path = self._suggested_output_path()
        self.log_signal.emit(f"[CORRECT] Gemini produced malformed tool payload(s): {names}; requiring a clean write path.")
        correction = (
            "LOCAL AGENT TOOL PAYLOAD ERROR:\n"
            "The previous write/append payload did not put a valid file path on the first payload line, or it copied placeholder text.\n"
            "Reply again with exactly one clean write block. No prose outside the block.\n\n"
            f"Use this exact target path on the first payload line: {target_path}\n"
            "The remaining lines must be the real complete file content, not instructions or placeholders.\n"
            "For a website/webpage, start the content with <!DOCTYPE html> and end with </html>.\n\n"
            f"USER REQUEST:\n{self.current_user_text.strip()}"
        )
        self.status_signal.emit("Correcting Gemini tool payload")
        self.browser.send_prompt(correction, [], callback=lambda result: self._after_send(result))

    def _suggested_output_path(self) -> Path:
        explicit_dir = self._explicit_destination_dir(self.current_user_text)
        base_dir = explicit_dir or self._default_user_output_dir()
        if self._looks_like_plain_file_request(self.current_user_text):
            filename = self._plain_file_filename(self.current_user_text, "")
        elif self._looks_like_artifact_request(self.current_user_text):
            filename = self._artifact_filename(self.current_user_text, "", "html", "<!DOCTYPE html>\n<html></html>")
        else:
            filename = "gemini-output.txt"
        return self._unique_artifact_path(base_dir / filename)

    def _maybe_save_local_artifact(self, reply: str) -> str:
        candidate = (
            self._extract_code_artifact(self.current_user_text, reply)
            or self._extract_plain_file_artifact(self.current_user_text, reply)
            or self._extract_synthetic_html_artifact(self.current_user_text, reply)
        )
        if not candidate:
            return ""
        filename, body = candidate
        if "LOCAL AGENT ENFORCEMENT:" in body:
            return ""
        target_path = self._unique_artifact_path(self._artifact_recovery_base_dir(filename) / filename)
        call = ToolCall("write", f"{target_path}\n{body}", "~@write@~", 0)
        self.log_signal.emit(f"[ARTIFACT] Gemini returned an app artifact instead of a write tool; saving {target_path.name}.")
        result = self.gateway.execute(call)
        self.history.add_event(
            "tool",
            {
                "name": "write",
                "raw": call.raw,
                "result": result.text,
                "source": "auto_artifact_recovery",
            },
        )
        self.tool_result_signal.emit(f"write\n{result.text}")
        return result.text

    def _artifact_recovery_base_dir(self, filename: str) -> Path:
        explicit_dir = self._explicit_destination_dir(self.current_user_text)
        if explicit_dir:
            return explicit_dir
        if self._looks_like_create_file_request(self.current_user_text):
            return self._default_user_output_dir()
        session_id = self.history.active_session_id or self.history.ensure_session()
        return self.gateway.data_dir / "artifacts" / session_id

    def _default_user_output_dir(self) -> Path:
        configured = str(self.config.get("agent", {}).get("default_output_dir") or "").strip()
        if configured:
            return Path(configured).expanduser()
        user_profile = Path.home()
        desktop = user_profile / "Desktop"
        return desktop if desktop.exists() else user_profile

    @staticmethod
    def _explicit_destination_dir(user_text: str) -> Path | None:
        text = str(user_text or "")
        match = re.search(r"(?:under|in|inside|at|to|on)\s+((?:[A-Za-z]:\\|\\\\|/)[^\n\r]+)", text, re.IGNORECASE)
        if not match:
            return None
        raw = match.group(1).strip().strip('"').strip("'")
        raw = re.sub(r"\s+(?:called|named|with|containing|saying|say|please)\b.*$", "", raw, flags=re.IGNORECASE).strip()
        if not raw:
            return None
        path = Path(raw).expanduser()
        if path.suffix:
            return path.parent
        return path

    @staticmethod
    def _unique_artifact_path(path: Path) -> Path:
        if not path.exists():
            return path
        stem = path.stem
        suffix = path.suffix
        for index in range(2, 1000):
            candidate = path.with_name(f"{stem}-{index}{suffix}")
            if not candidate.exists():
                return candidate
        return path.with_name(f"{stem}-{uuid.uuid4().hex[:8]}{suffix}")

    @classmethod
    def _extract_code_artifact(cls, user_text: str, reply: str) -> tuple[str, str] | None:
        if not cls._looks_like_artifact_request(user_text):
            return None
        blocks = cls._code_blocks(reply)
        if not blocks:
            return None
        language, code = cls._best_code_block(blocks)
        if len(code.strip()) < 50:
            return None
        filename = cls._artifact_filename(user_text, reply, language, code)
        return filename, code.strip() + "\n"

    @classmethod
    def _extract_synthetic_html_artifact(cls, user_text: str, reply: str) -> tuple[str, str] | None:
        if not cls._looks_like_webpage_request(user_text):
            return None
        if cls._code_blocks(reply):
            return None
        content = cls._website_brief_from_reply(reply)
        if len(re.findall(r"\w+", content)) < 35:
            return None
        filename = cls._artifact_filename(user_text, reply, "html", "<!DOCTYPE html>\n<html></html>")
        return filename, cls._build_html_from_brief(user_text, content)

    @classmethod
    def _extract_plain_file_artifact(cls, user_text: str, reply: str) -> tuple[str, str] | None:
        if not cls._looks_like_plain_file_request(user_text):
            return None
        content = cls._plain_file_content_from_request(user_text)
        if not content:
            return None
        filename = cls._plain_file_filename(user_text, reply)
        return filename, content.rstrip() + "\n"

    @staticmethod
    def _looks_like_artifact_request(user_text: str) -> bool:
        text = str(user_text or "").lower()
        action = any(word in text for word in ["create", "build", "make", "generate", "write", "save"])
        target = any(
            word in text
            for word in [
                "website",
                "web site",
                "webpage",
                "web page",
                "landing page",
                "html",
                "file",
                "script",
                "page",
            ]
        )
        return action and target

    @staticmethod
    def _looks_like_webpage_request(user_text: str) -> bool:
        text = str(user_text or "").lower()
        action = any(word in text for word in ["create", "build", "make", "generate", "write", "save"])
        target = any(word in text for word in ["website", "web site", "webpage", "web page", "landing page", "html"])
        return action and target

    @classmethod
    def _website_brief_from_reply(cls, reply: str) -> str:
        text = str(reply or "").replace("\r\n", "\n").replace("\r", "\n")
        text = re.split(r"\nTOOL RESULTS FOR TURN\b", text, maxsplit=1, flags=re.IGNORECASE)[0]
        marker_index = text.lower().rfind("gemini said")
        if marker_index >= 0:
            text = text[marker_index + len("gemini said") :]
        lines: list[str] = []
        skip_exact = {
            "sign in",
            "gemini",
            "about gemini",
            "opens in a new window",
            "gemini app",
            "subscriptions",
            "for business",
            "new chat",
            "conversation with gemini",
            "google search",
            "query successful",
            "try again without apps",
            "tools",
            "fast",
            "pro",
            "thinking",
            "gemini is ai and can make mistakes.",
            "export to sheets",
        }
        for raw_line in text.splitlines():
            line = raw_line.strip()
            lowered = line.lower()
            if not line or lowered in skip_exact:
                continue
            if lowered.startswith("you said"):
                continue
            if re.fullmatch(r"\[tool_result:[^\]]+\]", line, flags=re.IGNORECASE):
                continue
            lines.append(line)
        return "\n".join(lines).strip()

    @classmethod
    def _build_html_from_brief(cls, user_text: str, brief: str) -> str:
        title = cls._title_from_request(user_text)
        sections = cls._brief_sections(brief)
        cards = sections[:6] or [("Overview", brief)]
        feature_cards = "\n".join(
            (
                '        <article class="card">\n'
                f"          <h3>{html.escape(title_text)}</h3>\n"
                f"          <p>{html.escape(cls._compact_text(body, 340))}</p>\n"
                "        </article>"
            )
            for title_text, body in cards
        )
        detail_sections = "\n".join(
            (
                '      <section class="content-section">\n'
                f"        <h2>{html.escape(title_text)}</h2>\n"
                f"        <p>{html.escape(cls._compact_text(body, 900))}</p>\n"
                "      </section>"
            )
            for title_text, body in sections[:5]
        )
        return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>{html.escape(title)}</title>
  <style>
    :root {{
      --ink: #111827;
      --muted: #64748b;
      --paper: #f8fafc;
      --panel: #ffffff;
      --linux: #16a34a;
      --windows: #2563eb;
      --accent: #f59e0b;
    }}
    * {{ box-sizing: border-box; }}
    body {{
      margin: 0;
      font-family: "Segoe UI", Tahoma, Geneva, Verdana, sans-serif;
      color: var(--ink);
      background:
        radial-gradient(circle at top left, rgba(37, 99, 235, 0.18), transparent 34rem),
        radial-gradient(circle at top right, rgba(22, 163, 74, 0.20), transparent 30rem),
        var(--paper);
      line-height: 1.6;
    }}
    header {{
      padding: 28px min(6vw, 72px);
      display: flex;
      justify-content: space-between;
      align-items: center;
      gap: 20px;
    }}
    .brand {{
      font-weight: 800;
      letter-spacing: -0.04em;
      font-size: clamp(1.4rem, 3vw, 2rem);
    }}
    nav a {{
      color: var(--muted);
      text-decoration: none;
      margin-left: 18px;
      font-weight: 700;
    }}
    .hero {{
      padding: 84px min(6vw, 72px) 72px;
      display: grid;
      grid-template-columns: minmax(0, 1.1fr) minmax(280px, 0.9fr);
      gap: 42px;
      align-items: center;
    }}
    .eyebrow {{
      color: var(--windows);
      text-transform: uppercase;
      font-size: 0.8rem;
      font-weight: 900;
      letter-spacing: 0.18em;
    }}
    h1 {{
      font-size: clamp(2.8rem, 7vw, 6rem);
      line-height: 0.92;
      margin: 14px 0 20px;
      letter-spacing: -0.08em;
    }}
    .hero p {{
      color: var(--muted);
      max-width: 62ch;
      font-size: 1.12rem;
    }}
    .cta-row {{ display: flex; flex-wrap: wrap; gap: 12px; margin-top: 28px; }}
    .button {{
      display: inline-block;
      padding: 12px 18px;
      border-radius: 999px;
      background: var(--ink);
      color: white;
      text-decoration: none;
      font-weight: 800;
    }}
    .button.secondary {{
      background: white;
      color: var(--ink);
      border: 1px solid #dbe3ef;
    }}
    .terminal {{
      background: #0f172a;
      color: #d1fae5;
      border-radius: 28px;
      padding: 22px;
      box-shadow: 0 28px 80px rgba(15, 23, 42, 0.22);
      border: 1px solid rgba(255,255,255,0.1);
    }}
    .terminal pre {{
      white-space: pre-wrap;
      font-size: 0.92rem;
      margin: 0;
    }}
    main {{ padding: 0 min(6vw, 72px) 72px; }}
    .grid {{
      display: grid;
      grid-template-columns: repeat(auto-fit, minmax(240px, 1fr));
      gap: 18px;
      margin: 20px 0 54px;
    }}
    .card, .content-section {{
      background: rgba(255,255,255,0.78);
      border: 1px solid rgba(148, 163, 184, 0.28);
      border-radius: 24px;
      padding: 24px;
      backdrop-filter: blur(16px);
    }}
    .card h3, .content-section h2 {{ margin-top: 0; letter-spacing: -0.03em; }}
    .card:nth-child(odd) {{ border-top: 5px solid var(--linux); }}
    .card:nth-child(even) {{ border-top: 5px solid var(--windows); }}
    .content-section {{ margin-bottom: 18px; }}
    footer {{
      padding: 28px min(6vw, 72px);
      color: var(--muted);
      border-top: 1px solid #e2e8f0;
    }}
    @media (max-width: 820px) {{
      .hero {{ grid-template-columns: 1fr; padding-top: 42px; }}
      nav {{ display: none; }}
    }}
  </style>
</head>
<body>
  <header>
    <div class="brand">AdminOps Hub</div>
    <nav>
      <a href="#tools">Tools</a>
      <a href="#security">Security</a>
      <a href="#platforms">Platforms</a>
    </nav>
  </header>
  <section class="hero">
    <div>
      <div class="eyebrow">Linux + Windows system administration</div>
      <h1>{html.escape(title)}</h1>
      <p>{html.escape(cls._compact_text(brief, 360))}</p>
      <div class="cta-row">
        <a class="button" href="#tools">Explore admin tools</a>
        <a class="button secondary" href="#security">Review hardening</a>
      </div>
    </div>
    <aside class="terminal" aria-label="Command examples">
      <pre>$ systemctl status nginx
PS> Get-Service | Where-Object Status -eq Running
$ ansible-playbook hardening.yml
PS> Test-NetConnection dc01 -Port 389</pre>
    </aside>
  </section>
  <main>
    <section id="tools">
      <div class="grid">
{feature_cards}
      </div>
    </section>
{detail_sections}
  </main>
  <footer>
    Built as a local Gemini Agent artifact for practical Linux and Windows administration.
  </footer>
</body>
</html>
"""

    @staticmethod
    def _title_from_request(user_text: str) -> str:
        text = str(user_text or "").strip()
        text = re.sub(r"^(create|build|make|generate|write)\s+(a\s+|an\s+)?(website|webpage|web site|web page)\s+(for|about)?\s*", "", text, flags=re.IGNORECASE)
        text = text.strip(" .:-") or "Linux and Windows Sysadmin Hub"
        return " ".join(word.capitalize() if word.lower() not in {"and", "for", "of"} else word.lower() for word in text.split())

    @staticmethod
    def _brief_sections(brief: str) -> list[tuple[str, str]]:
        sections: list[tuple[str, str]] = []
        current_title = "Overview"
        current_lines: list[str] = []
        heading_re = re.compile(r"^(?:\d+\.\s*)?([A-Z][A-Za-z0-9 /&()\"'-]{3,80})$")
        for line in str(brief or "").splitlines():
            cleaned = line.strip()
            if not cleaned:
                continue
            heading = heading_re.match(cleaned)
            if heading and len(cleaned.split()) <= 8:
                if current_lines:
                    sections.append((current_title, " ".join(current_lines)))
                current_title = re.sub(r"^\d+\.\s*", "", cleaned).strip()
                current_lines = []
            else:
                current_lines.append(cleaned)
        if current_lines:
            sections.append((current_title, " ".join(current_lines)))
        return sections[:8]

    @staticmethod
    def _compact_text(text: str, max_chars: int) -> str:
        value = re.sub(r"\s+", " ", str(text or "")).strip()
        if len(value) <= max_chars:
            return value
        cut = value[: max_chars - 1].rsplit(" ", 1)[0].rstrip(".,;:")
        return f"{cut}."

    @staticmethod
    def _looks_like_plain_file_request(user_text: str) -> bool:
        text = str(user_text or "").lower()
        has_action = any(word in text for word in ["create", "make", "write", "save"])
        has_file = any(word in text for word in ["text file", "txt file", ".txt", "file"])
        has_content_hint = any(phrase in text for phrase in ["inside it", "in it", "with text", "with the text", "containing", "saying", "type"]) or bool(
            re.search(r"\bsay\s+\S+", text)
        )
        return has_action and has_file and has_content_hint

    @staticmethod
    def _plain_file_content_from_request(user_text: str) -> str:
        text = str(user_text or "").strip()
        patterns = [
            r"(?:inside it|in it|into it)\s+(.+)$",
            r"(?:with the text|with text|containing|saying)\s+(.+)$",
            r"\bsay\s+(.+)$",
            r"(?:type|write|put)\s+(?:inside it|in it|into it)?\s*(.+)$",
        ]
        for pattern in patterns:
            match = re.search(pattern, text, re.IGNORECASE | re.DOTALL)
            if match:
                content = match.group(1).strip().strip('"').strip("'")
                content = re.sub(r"\s+(?:please|thanks|thank you)$", "", content, flags=re.IGNORECASE).strip()
                if content:
                    return content
        return ""

    @staticmethod
    def _plain_file_filename(user_text: str, reply: str) -> str:
        combined = f"{user_text}\n{reply}"
        explicit = re.search(r"(?:^|[\s`\"'(:])([A-Za-z0-9_-]+(?:\.[A-Za-z0-9_-]+)*\.txt)\b", combined, re.IGNORECASE)
        if explicit:
            return Path(explicit.group(1).strip()).name
        named = re.search(r"(?:called|named|name(?:d)? it|save it as)\s+([A-Za-z0-9_-]+)(?:\.txt)?\b", combined, re.IGNORECASE)
        if named:
            return f"{named.group(1).strip()}.txt"
        ready_cards = [
            match.group(1).strip()
            for match in re.finditer(r"\b([A-Za-z0-9_-]{2,64})\s+TXT\b", reply, re.IGNORECASE)
            if match.group(1).strip().lower() not in {"your", "the", "a", "this"}
        ]
        if ready_cards:
            return f"{ready_cards[-1]}.txt"
        return "note.txt"

    @staticmethod
    def _code_blocks(reply: str) -> list[tuple[str, str]]:
        text = str(reply or "")
        blocks = [(match.group(1).strip().lower(), match.group(2)) for match in re.finditer(r"```([A-Za-z0-9_+-]*)\s*\n([\s\S]*?)```", text)]
        if blocks:
            return blocks
        html_match = re.search(r"(<!DOCTYPE html>[\s\S]+)", text, re.IGNORECASE)
        if html_match:
            return [("html", html_match.group(1))]
        return []

    @staticmethod
    def _best_code_block(blocks: list[tuple[str, str]]) -> tuple[str, str]:
        preferred = {"html": 100, "htm": 95, "javascript": 80, "js": 80, "css": 70}
        return sorted(blocks, key=lambda item: (preferred.get(item[0], 0), len(item[1])), reverse=True)[0]

    @staticmethod
    def _artifact_filename(user_text: str, reply: str, language: str, code: str) -> str:
        combined = f"{user_text}\n{reply}"
        match = re.search(r"(?:^|[\s`\"'(:])([A-Za-z0-9_-]+(?:\.[A-Za-z0-9_-]+)*\.(?:html|htm|css|js|mjs|py|md|txt|json|csv|xml|svg))\b", combined, re.IGNORECASE)
        if match:
            filename = Path(match.group(1).strip()).name
        elif "<!doctype html" in code.lower() or "<html" in code.lower() or language in {"html", "htm"}:
            filename = "index.html"
        else:
            filename = {
                "css": "style.css",
                "javascript": "script.js",
                "js": "script.js",
                "python": "script.py",
                "py": "script.py",
                "markdown": "README.md",
                "md": "README.md",
                "json": "data.json",
                "svg": "image.svg",
            }.get(language, "artifact.txt")
        filename = re.sub(r"[^A-Za-z0-9_. -]", "_", filename).strip(" .")
        return filename or "artifact.txt"

    def _correct_tool_refusal(self, reply: str) -> None:
        self.current_loop_count += 1
        max_loops = int(self.config.get("agent", {}).get("max_tool_loops", 12))
        if self.current_loop_count > max_loops:
            self.final_signal.emit(reply)
            self._finish_turn("Ready")
            return
        self.log_signal.emit("[GUIDE] Gemini answered as plain web chat; sending tool correction.")
        correction = (
            f"{self.tool_contract()}\n\n"
            "CORRECTION:\n"
            "Your previous answer treated this as plain Gemini without local tools. In this app, you DO have local computer access through the sigil tools.\n"
            "For the user's request below, do not provide keyboard/manual instructions. Emit the correct tool call now.\n\n"
            f"USER REQUEST:\n{self.current_user_text.strip()}"
        )
        self.status_signal.emit("Teaching Gemini local tools")
        self.browser.send_prompt(correction, [], callback=lambda result: self._after_send(result))

    def _execute_tools_worker(self, calls: list[ToolCall]) -> None:
        results: list[tuple[ToolCall, str]] = []
        for call in calls:
            count = self.call_counts.get(call.signature, 0) + 1
            self.call_counts[call.signature] = count
            if count > 1:
                result = "[BLOCKED] Duplicate tool call detected in this turn."
            else:
                gateway_result: GatewayResult = self.gateway.execute(call)
                result = gateway_result.text
            self.history.add_event("tool", {"name": call.name, "raw": call.raw, "result": result})
            results.append((call, result))
        self._tool_results_ready.emit(self.current_turn_id, results)

    @Slot(str, object)
    def _send_tool_results(self, turn_id: str, results: list[tuple[ToolCall, str]]) -> None:
        if not self.running:
            return
        for call, result in results:
            self.tool_result_signal.emit(f"{call.name}\n{result}")
        message = build_tool_result_message(turn_id, results)
        self.status_signal.emit("Returning tool results to Gemini")
        self.browser.send_prompt(message, [], callback=lambda result: self._after_send(result))
