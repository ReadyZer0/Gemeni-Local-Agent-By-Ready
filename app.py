from __future__ import annotations

import json
import os
import sys
import uuid
from pathlib import Path

from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QAction
from PySide6.QtWidgets import (
    QApplication,
    QFileDialog,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMainWindow,
    QPushButton,
    QComboBox,
    QSplitter,
    QTextEdit,
    QToolBar,
    QVBoxLayout,
    QWidget,
)

from agent_loop import GeminiAgentLoop
from approvals import ApprovalBridge
from attachments import AttachmentManager
from gemini_browser import GeminiBrowser
from history_store import HistoryStore
from mcp_gateway import ToolGateway
from tool_protocol import ToolCall


PROJECT_DIR = Path(__file__).resolve().parent
CONFIG_PATH = PROJECT_DIR / "config.json"
FEATURE_MODES = {
    "canvas": {
        "button": "Canvas",
        "display": "Canvas",
        "labels": ["canvas", "open canvas", "create canvas"],
        "prompt_prefix": "Use Gemini Canvas for this request. Keep the Canvas visible and create or edit the artifact there when useful.",
    },
    "nano_banana": {
        "button": "Nano Banana",
        "display": "Nano Banana image",
        "labels": ["nano banana", "create image", "generate image", "image generation", "imagen"],
        "prompt_prefix": "Use Gemini's image generation mode, including Nano Banana/Create image if available, for this request.",
    },
    "music": {
        "button": "Music",
        "display": "Create music",
        "labels": ["create music", "music", "generate music"],
        "prompt_prefix": "Use Gemini's Create music mode for this request.",
    },
    "video": {
        "button": "Video",
        "display": "Create video",
        "labels": ["create video", "video", "generate video"],
        "prompt_prefix": "Use Gemini's Create video mode for this request.",
    },
}
MODEL_MODES = {
    "fast": {
        "display": "Fast",
        "labels": ["fast"],
    },
    "pro": {
        "display": "Pro",
        "labels": ["pro"],
    },
    "thinking": {
        "display": "Thinking",
        "labels": ["thinking", "think", "deep think"],
    },
}
GEMINI_MEMORY_INSTRUCTIONS = """Inside Gemini Local Agent, I can use local computer tools by writing tool-call blocks for the desktop app.

For local actions, do not give manual instructions or say I cannot access the computer. Use a real enabled tool name inside a sigil block:
~@write@~
C:\\Users\\awoen\\Desktop\\example.txt
file content
~@exit@~

Useful tools: read, explorer, write, append, replace, mkdir, delete, copy, move, terminal, powershell, wsl, fetch, browser_open, browser_inspect, browser_click, browser_type, git_status, git_diff, memory_get, memory_put, window_list, window_focus, gui_click, gui_type, gui_hotkey, blender_python, excel_read, excel_write.

For write/append, the first payload line is the file path and the remaining lines are the content. Never use placeholder names such as tool or tool_name. If a path is unknown, use explorer first. Wait for tool results before saying the task is done."""


def load_config() -> dict:
    return json.loads(CONFIG_PATH.read_text(encoding="utf-8"))


def save_config(config: dict) -> None:
    CONFIG_PATH.write_text(json.dumps(config, indent=2), encoding="utf-8")


class GeminiWorkspace(QWidget):
    """Keeps the local composer visually attached to the Gemini browser."""

    def __init__(self, browser: GeminiBrowser, composer: QWidget, parent=None):
        super().__init__(parent)
        self.browser = browser
        self.composer = composer
        self.web_composer_rect: dict | None = None
        self.browser.setParent(self)
        self.composer.setParent(self)
        self.setMinimumWidth(620)
        self.browser.composer_rect_changed.connect(self.set_web_composer_rect)
        self.composer.raise_()

    def resizeEvent(self, event) -> None:  # noqa: N802 - Qt override
        self._position_children()
        super().resizeEvent(event)

    def set_web_composer_rect(self, rect: dict) -> None:
        self.web_composer_rect = rect
        self._position_children()

    def _position_children(self) -> None:
        self.browser.setGeometry(self.rect())
        composer_height = self.composer.sizeHint().height()
        rect = self.web_composer_rect or {}
        native_width = int(float(rect.get("width", 0) or 0))
        native_height = int(float(rect.get("height", 0) or 0))
        if native_width >= 320 and native_height >= 60:
            composer_width = min(max(320, native_width), max(320, self.width() - 16))
            native_x = int(float(rect.get("x", 0) or 0))
            native_y = int(float(rect.get("y", 0) or 0))
            x = max(8, min(native_x, self.width() - composer_width - 8))
            y = max(8, min(native_y + ((native_height - composer_height) // 2), self.height() - composer_height - 8))
        else:
            margin = 24
            available_width = max(320, self.width() - (margin * 2))
            composer_width = min(860, available_width)
            x = max(8, (self.width() - composer_width) // 2)
            y = max(8, self.height() - composer_height - 24)
        self.composer.setGeometry(x, y, composer_width, composer_height)
        self.composer.raise_()


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.config = load_config()
        self.setWindowTitle(self.config.get("app", {}).get("name", "Gemini Local Agent"))
        self.resize(1450, 900)
        self.pending_files: list[str] = []
        self.active_feature_key = ""
        self.feature_buttons: dict[str, QPushButton] = {}
        self.tool_seed_in_progress = False

        data_dir = PROJECT_DIR / "data"
        self.history = HistoryStore(data_dir)
        self.history.ensure_session(self.config.get("gemini", {}).get("session_mode", "persistent_thread"))
        self.history.update_metadata(tool_contract_seed_in_progress=False)
        self.attachments = AttachmentManager(data_dir / "attachments")
        self.approvals = ApprovalBridge(self)
        self.browser = GeminiBrowser(self.config, PROJECT_DIR, self)
        self.gateway = ToolGateway(self.config, PROJECT_DIR, approval_callback=self._approval_callback)
        self.agent = GeminiAgentLoop(self.config, self.browser, self.gateway, self.history, self.attachments, self)

        self._build_ui()
        self._connect_signals()
        self._apply_theme()
        QTimer.singleShot(2500, self.auto_seed_tools_if_needed)

    def _build_ui(self) -> None:
        toolbar = QToolBar("Controls")
        toolbar.setMovable(False)
        self.addToolBar(toolbar)

        new_session = QAction("New Session", self)
        new_session.triggered.connect(self.new_session)
        toolbar.addAction(new_session)

        open_gemini = QAction("Open Gemini", self)
        open_gemini.triggered.connect(self.browser.load_gemini)
        toolbar.addAction(open_gemini)

        seed_tools = QAction("Reseed Tools", self)
        seed_tools.triggered.connect(self.seed_tools)
        toolbar.addAction(seed_tools)

        run_visible = QAction("Run Visible Tools", self)
        run_visible.triggered.connect(self.run_visible_tools)
        toolbar.addAction(run_visible)

        copy_memory = QAction("Copy Gemini Memory", self)
        copy_memory.triggered.connect(self.copy_gemini_memory)
        toolbar.addAction(copy_memory)

        toolbar.addWidget(QLabel(" Session "))
        self.session_mode = QComboBox()
        self.session_mode.addItems(["persistent_thread", "fresh_per_task"])
        self.session_mode.setCurrentText(self.config.get("gemini", {}).get("session_mode", "persistent_thread"))
        self.session_mode.currentTextChanged.connect(self.set_session_mode)
        toolbar.addWidget(self.session_mode)

        toolbar.addWidget(QLabel(" Approval "))
        self.approval_policy = QComboBox()
        self.approval_policy.addItems(["guarded", "mostly_auto", "full_auto"])
        self.approval_policy.setCurrentText(self.config.get("app", {}).get("approval_policy", "guarded"))
        self.approval_policy.currentTextChanged.connect(self.set_approval_policy)
        toolbar.addWidget(self.approval_policy)

        toolbar.addWidget(QLabel(" Access "))
        self.access_mode = QComboBox()
        self.access_mode.addItems(["restricted", "ask", "full"])
        self.access_mode.setCurrentText(self.config.get("security", {}).get("access_mode", "restricted"))
        self.access_mode.currentTextChanged.connect(self.set_access_mode)
        toolbar.addWidget(self.access_mode)

        toolbar.addWidget(QLabel(" Agentic "))
        self.agentic_mode = QComboBox()
        self.agentic_mode.addItems(["force_tools", "observe"])
        force_tools = bool(self.config.get("agent", {}).get("force_tool_calls_for_local_actions", True))
        self.agentic_mode.setCurrentText("force_tools" if force_tools else "observe")
        self.agentic_mode.currentTextChanged.connect(self.set_agentic_mode)
        toolbar.addWidget(self.agentic_mode)

        toolbar.addWidget(QLabel(" Model "))
        self.model_mode = QComboBox()
        self.model_mode.addItems(list(MODEL_MODES.keys()))
        self.model_mode.setCurrentText(self.config.get("gemini", {}).get("model_mode", "fast"))
        self.model_mode.currentTextChanged.connect(self.set_model_mode)
        toolbar.addWidget(self.model_mode)

        splitter = QSplitter(Qt.Horizontal)
        self.setCentralWidget(splitter)

        left = QWidget()
        left.setMinimumWidth(300)
        left_layout = QVBoxLayout(left)
        left_layout.setContentsMargins(10, 10, 10, 10)

        activity_title = QLabel("Activity")
        activity_title.setObjectName("PanelTitle")
        left_layout.addWidget(activity_title)

        self.status_label = QLabel("Ready")
        self.status_label.setObjectName("StatusLabel")
        left_layout.addWidget(self.status_label)

        self.chat = QTextEdit()
        self.chat.setReadOnly(True)
        self.chat.setObjectName("Chat")
        left_layout.addWidget(self.chat, 3)

        self.logs = QTextEdit()
        self.logs.setReadOnly(True)
        self.logs.setObjectName("Logs")
        self.logs.setMaximumHeight(190)
        left_layout.addWidget(self.logs, 1)

        composer = self._build_composer()
        gemini_workspace = GeminiWorkspace(self.browser, composer)

        splitter.addWidget(left)
        splitter.addWidget(gemini_workspace)
        splitter.setSizes([370, 1080])
        self.input.setFocus()

    def _build_composer(self) -> QFrame:
        composer = QFrame()
        composer.setObjectName("UnifiedComposer")
        composer.setMinimumHeight(150)
        composer.setMaximumHeight(180)

        composer_layout = QVBoxLayout(composer)
        composer_layout.setContentsMargins(18, 12, 18, 12)
        composer_layout.setSpacing(7)

        self.input = QLineEdit()
        self.input.setObjectName("UnifiedInput")
        self.input.setPlaceholderText("Ask Gemini...")
        self.input.returnPressed.connect(self.send_message)
        composer_layout.addWidget(self.input)

        feature_row = QHBoxLayout()
        feature_row.setSpacing(8)

        self.feature_status = QLabel("Mode: Chat")
        self.feature_status.setObjectName("FeatureStatus")
        feature_row.addWidget(self.feature_status, 1)

        for key, feature in FEATURE_MODES.items():
            feature_btn = QPushButton(feature["button"])
            feature_btn.setObjectName("FeatureButton")
            feature_btn.setCursor(Qt.PointingHandCursor)
            feature_btn.setToolTip(f"Activate {feature['display']} mode")
            feature_btn.clicked.connect(lambda _checked=False, mode_key=key: self.activate_feature_mode(mode_key))
            self.feature_buttons[key] = feature_btn
            feature_row.addWidget(feature_btn)

        composer_layout.addLayout(feature_row)

        action_row = QHBoxLayout()
        action_row.setSpacing(8)

        self.attachment_label = QLabel("No attachments")
        self.attachment_label.setObjectName("AttachmentLabel")
        action_row.addWidget(self.attachment_label, 1)

        attach_btn = QPushButton("Attach")
        attach_btn.setObjectName("ComposerAction")
        attach_btn.setCursor(Qt.PointingHandCursor)
        attach_btn.clicked.connect(self.attach_files)
        action_row.addWidget(attach_btn)

        paste_image_btn = QPushButton("Paste Image")
        paste_image_btn.setObjectName("ComposerAction")
        paste_image_btn.setCursor(Qt.PointingHandCursor)
        paste_image_btn.clicked.connect(self.paste_clipboard_image)
        action_row.addWidget(paste_image_btn)

        send_btn = QPushButton("Send")
        send_btn.setObjectName("PrimaryComposerAction")
        send_btn.setCursor(Qt.PointingHandCursor)
        send_btn.clicked.connect(self.send_message)
        action_row.addWidget(send_btn)

        stop_btn = QPushButton("Stop")
        stop_btn.setObjectName("ComposerAction")
        stop_btn.setCursor(Qt.PointingHandCursor)
        stop_btn.clicked.connect(self.agent.stop)
        action_row.addWidget(stop_btn)

        composer_layout.addLayout(action_row)
        return composer

    def _connect_signals(self) -> None:
        self.browser.status_changed.connect(self.add_log)
        self.browser.url_changed_text.connect(lambda url: self.history.update_metadata(gemini_url=url))
        self.agent.log_signal.connect(self.add_log)
        self.agent.status_signal.connect(self.set_status)
        self.agent.final_signal.connect(lambda text: self.add_chat("Gemini", text))
        self.agent.tool_result_signal.connect(lambda text: self.add_log(f"[TOOL RESULT]\n{text}"))

    def _apply_theme(self) -> None:
        self.setStyleSheet(
            """
            QMainWindow, QWidget { background: #080808; color: #e5e7eb; font-family: Segoe UI; }
            QTextEdit, QLineEdit { background: #050505; color: #e5e7eb; border: 1px solid #262626; border-radius: 6px; padding: 8px; }
            QPushButton, QComboBox { background: #111111; color: #f4c542; border: 1px solid #333333; border-radius: 6px; padding: 7px 10px; }
            QPushButton:hover { background: #181818; }
            QToolBar { background: #050505; border-bottom: 1px solid #262626; spacing: 8px; }
            #PanelTitle { color: #e5e7eb; font-size: 15px; font-weight: 700; padding: 2px 6px; }
            #StatusLabel { color: #10b981; font-weight: bold; padding: 6px; }
            #AttachmentLabel { color: #94a3b8; padding: 4px; }
            #FeatureStatus { color: #cbd5e1; padding: 4px; }
            #Logs { color: #86efac; font-family: Consolas; font-size: 11px; }
            #UnifiedComposer { background: #202124; border: 1px solid #303134; border-radius: 30px; }
            #UnifiedInput { background: transparent; border: none; color: #f8fafc; font-size: 15px; padding: 8px 4px; }
            QPushButton#FeatureButton { background: #17191c; color: #dbeafe; border: 1px solid #3b4250; border-radius: 14px; padding: 5px 10px; }
            QPushButton#FeatureButton[active="true"] { background: #f4c542; color: #111111; border: 1px solid #f4c542; font-weight: 700; }
            QPushButton#ComposerAction { background: #26272a; color: #e5e7eb; border: 1px solid #3a3b3f; border-radius: 16px; padding: 6px 12px; }
            QPushButton#PrimaryComposerAction { background: #f4c542; color: #111111; border: 1px solid #f4c542; border-radius: 16px; padding: 6px 14px; font-weight: 700; }
            """
        )

    def _approval_callback(self, title: str, content: str, editable: bool, metadata: dict) -> tuple[bool, str]:
        decision = self.approvals.ask(title, content, editable, metadata)
        return decision.approved, decision.edited_content

    def set_session_mode(self, value: str) -> None:
        self.config.setdefault("gemini", {})["session_mode"] = value
        save_config(self.config)
        self.history.update_metadata(session_mode=value)
        self.add_log(f"[CONFIG] Session mode set to {value}.")

    def set_approval_policy(self, value: str) -> None:
        self.config.setdefault("app", {})["approval_policy"] = value
        save_config(self.config)
        self.gateway.config = self.config
        self.add_log(f"[CONFIG] Approval policy set to {value}.")

    def set_access_mode(self, value: str) -> None:
        self.config.setdefault("security", {})["access_mode"] = value
        save_config(self.config)
        self.gateway.config = self.config
        self.add_log(f"[CONFIG] Filesystem access mode set to {value}.")

    def set_agentic_mode(self, value: str) -> None:
        enabled = value == "force_tools"
        self.config.setdefault("agent", {})["force_tool_calls_for_local_actions"] = enabled
        save_config(self.config)
        self.agent.config = self.config
        self.add_log(f"[CONFIG] Agentic tool enforcement {'enabled' if enabled else 'disabled'}.")

    def set_model_mode(self, value: str) -> None:
        mode = MODEL_MODES.get(value)
        if not mode:
            return
        self.config.setdefault("gemini", {})["model_mode"] = value
        save_config(self.config)
        self.add_log(f"[MODEL] Switching Gemini mode to {mode['display']}.")

        def handle_result(result):
            if isinstance(result, dict) and result.get("ok"):
                self.add_log(f"[MODEL] Gemini mode selected: {result.get('target')} via {result.get('opened')}.")
            else:
                self.add_log(f"[MODEL] Gemini mode control not found for {mode['display']}: {result}")

        self.browser.activate_model_mode(mode["labels"], handle_result)

    def new_session(self) -> None:
        sid = self.history.new_session(self.config.get("gemini", {}).get("session_mode", "persistent_thread"))
        self.pending_files = []
        self.refresh_attachments()
        self.chat.clear()
        self.logs.clear()
        self.add_log(f"[SESSION] New local session: {sid}")
        if self.config.get("gemini", {}).get("session_mode") == "fresh_per_task":
            self.browser.new_chat(lambda _result: QTimer.singleShot(1500, self.auto_seed_tools_if_needed))
        else:
            QTimer.singleShot(1000, self.auto_seed_tools_if_needed)

    def auto_seed_tools_if_needed(self) -> None:
        if not self.config.get("gemini", {}).get("auto_seed_tools_on_new_session", True):
            return
        if self.agent.running:
            QTimer.singleShot(2000, self.auto_seed_tools_if_needed)
            return
        if self.tool_seed_in_progress:
            return
        if self.history.metadata().get("tool_contract_seeded"):
            return
        self.seed_tools(auto=True)

    def seed_tools(self, auto: bool = False) -> None:
        if self.agent.running:
            if auto:
                QTimer.singleShot(2000, self.auto_seed_tools_if_needed)
            else:
                self.add_log("[SEED TOOLS] Wait for the current Gemini response before reseeding tools.")
            return
        if self.tool_seed_in_progress:
            self.add_log("[SEED TOOLS] Tool instructions are already being sent.")
            return
        self.tool_seed_in_progress = True
        self.history.update_metadata(tool_contract_seed_in_progress=True)
        contract = (
            self.agent.tool_contract()
            + "\n\nAcknowledge briefly that you can use these tools. For future local-action requests, call tools instead of giving manual instructions."
        )
        def handle_seed_result(result):
            self.tool_seed_in_progress = False
            self.history.update_metadata(tool_contract_seed_in_progress=False)
            ok = bool(isinstance(result, dict) and result.get("ok"))
            if ok:
                self.history.update_metadata(tool_contract_seeded=True)
                prefix = "AUTO SEED" if auto else "SEED TOOLS"
                self.add_log(f"[{prefix}] Gemini Local Agent tool contract sent.")
            else:
                prefix = "AUTO SEED" if auto else "SEED TOOLS"
                self.add_log(f"[{prefix}] Could not send tool contract yet: {result}")

        self.browser.send_prompt(contract, [], callback=handle_seed_result)
        if not auto:
            self.add_log("[SEED TOOLS] Sending Gemini Local Agent tool contract to the current Gemini chat.")

    def run_visible_tools(self) -> None:
        hint = self.input.text().strip() or self.latest_user_request_hint()
        self.browser.read_state(lambda state: self.agent.process_visible_state(state, hint))
        self.add_log("[VISIBLE] Reading current Gemini page for tool calls or recoverable artifacts.")

    def copy_gemini_memory(self) -> None:
        QApplication.clipboard().setText(GEMINI_MEMORY_INSTRUCTIONS)
        self.add_log("[CLIPBOARD] Compact Gemini Local Agent memory text copied.")

    def latest_user_request_hint(self) -> str:
        for event in reversed(self.history.events()):
            if event.get("kind") == "user":
                text = str((event.get("payload") or {}).get("text") or "").strip()
                if text:
                    return text
        return "manual visible Gemini reply"

    def activate_feature_mode(self, key: str) -> None:
        if key == self.active_feature_key:
            self.clear_feature_mode()
            return
        feature = FEATURE_MODES.get(key)
        if not feature:
            return
        self.active_feature_key = key
        self.update_feature_mode_ui()
        self.history.update_metadata(active_feature=key)
        self.add_log(f"[FEATURE] Activating Gemini {feature['display']} mode.")

        def handle_activation(result):
            if isinstance(result, dict) and result.get("ok"):
                self.add_log(f"[FEATURE] Gemini UI activated {feature['display']}: {result.get('method')} -> {result.get('target')}")
            else:
                self.add_log(f"[FEATURE] Gemini UI control not found for {feature['display']}; next send will include a mode instruction.")

        self.browser.activate_feature(feature["labels"], handle_activation)

    def clear_feature_mode(self, silent: bool = False) -> None:
        if not self.active_feature_key:
            return
        self.active_feature_key = ""
        self.update_feature_mode_ui()
        self.history.update_metadata(active_feature="")
        if not silent:
            self.add_log("[FEATURE] Back to normal chat mode.")

    def update_feature_mode_ui(self) -> None:
        active = self.active_feature_key
        for key, button in self.feature_buttons.items():
            button.setProperty("active", "true" if key == active else "false")
            button.style().unpolish(button)
            button.style().polish(button)
        if active:
            feature = FEATURE_MODES[active]
            self.feature_status.setText(f"Mode: {feature['display']} for next send")
            self.input.setPlaceholderText(f"Ask Gemini with {feature['display']}...")
        else:
            self.feature_status.setText("Mode: Chat")
            self.input.setPlaceholderText("Ask Gemini...")

    def enrich_with_active_feature(self, text: str) -> tuple[str, str]:
        feature_key = self.active_feature_key
        if not feature_key:
            return text, "You"
        feature = FEATURE_MODES.get(feature_key)
        if not feature:
            self.clear_feature_mode(silent=True)
            return text, "You"
        prompt = f"{feature['prompt_prefix']}\n\nUser request:\n{text}"
        sender = f"You [{feature['display']}]"
        self.clear_feature_mode(silent=True)
        return prompt, sender

    def attach_files(self) -> None:
        files, _ = QFileDialog.getOpenFileNames(self, "Attach files for Gemini")
        if files:
            self.pending_files.extend(files)
            self.refresh_attachments()

    def paste_clipboard_image(self) -> None:
        image = QApplication.clipboard().image()
        if image.isNull():
            self.add_log("[WARN] Clipboard does not contain an image.")
            return
        queue_dir = PROJECT_DIR / "data" / "attachments" / "_clipboard_queue"
        queue_dir.mkdir(parents=True, exist_ok=True)
        path = queue_dir / f"clipboard_{uuid.uuid4().hex}.png"
        if image.save(str(path), "PNG"):
            self.pending_files.append(str(path))
            self.refresh_attachments()
            self.add_log(f"[ATTACH] Clipboard image queued: {path.name}")
        else:
            self.add_log("[ERROR] Could not save clipboard image.")

    def refresh_attachments(self) -> None:
        if not self.pending_files:
            self.attachment_label.setText("No attachments")
            return
        names = ", ".join(Path(path).name for path in self.pending_files)
        self.attachment_label.setText(f"Queued attachments: {names}")

    def send_message(self) -> None:
        text = self.input.text().strip()
        if not text:
            return
        files = list(self.pending_files)
        self.pending_files = []
        self.refresh_attachments()
        self.input.clear()
        prompt, sender = self.enrich_with_active_feature(text)
        self.add_chat(sender, text)
        self.agent.start_task(prompt, files)

    def add_chat(self, sender: str, text: str) -> None:
        self.chat.append(f"<b>{sender}</b>")
        self.chat.append(self._escape(text).replace("\n", "<br>"))
        self.chat.append("")

    def add_log(self, text: str) -> None:
        self.logs.append(str(text))

    def set_status(self, text: str) -> None:
        self.status_label.setText(str(text))

    @staticmethod
    def _escape(text: str) -> str:
        return (
            str(text or "")
            .replace("&", "&amp;")
            .replace("<", "&lt;")
            .replace(">", "&gt;")
        )


def smoke_check() -> int:
    config = load_config()
    gateway = ToolGateway(config, PROJECT_DIR)
    status = gateway.execute(ToolCall("mcp_status", "check", "~@mcp_status@~", 0))
    if not status.ok:
        print(status.text)
        return 1
    print("smoke ok")
    print(status.text)
    return 0


def main() -> int:
    if "--smoke" in sys.argv:
        return smoke_check()
    os.environ.setdefault("QTWEBENGINE_CHROMIUM_FLAGS", "--disable-features=CalculateNativeWinOcclusion")
    app = QApplication(sys.argv)
    window = MainWindow()
    window.show()
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
