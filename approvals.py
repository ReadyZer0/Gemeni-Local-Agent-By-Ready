from __future__ import annotations

import threading
from dataclasses import dataclass
from typing import Any

from PySide6.QtCore import QObject, Signal, Slot, Qt
from PySide6.QtWidgets import QDialog, QHBoxLayout, QLabel, QPushButton, QTextEdit, QVBoxLayout


@dataclass
class ApprovalDecision:
    approved: bool
    edited_content: str


class ApprovalBridge(QObject):
    request_signal = Signal(object)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.request_signal.connect(self._show_dialog)

    def ask(self, title: str, content: str, editable: bool = True, metadata: dict[str, Any] | None = None) -> ApprovalDecision:
        event = threading.Event()
        request = {
            "title": title,
            "content": content,
            "editable": editable,
            "metadata": metadata or {},
            "approved": False,
            "edited_content": content,
            "event": event,
        }
        self.request_signal.emit(request)
        event.wait()
        return ApprovalDecision(bool(request["approved"]), str(request["edited_content"]))

    @Slot(object)
    def _show_dialog(self, request: dict[str, Any]) -> None:
        dialog = QDialog()
        dialog.setWindowTitle(str(request.get("title") or "Approval Required"))
        dialog.resize(780, 540)
        layout = QVBoxLayout(dialog)

        title = QLabel(str(request.get("title") or "Approval Required"))
        title.setStyleSheet("font-weight: bold; color: #f4c542;")
        layout.addWidget(title)

        metadata = request.get("metadata") or {}
        if metadata:
            details = QLabel("\n".join(f"{key}: {value}" for key, value in metadata.items()))
            details.setStyleSheet("color: #94a3b8; font-family: Consolas;")
            details.setWordWrap(True)
            layout.addWidget(details)

        editor = QTextEdit()
        editor.setPlainText(str(request.get("content") or ""))
        editor.setReadOnly(not bool(request.get("editable", True)))
        editor.setStyleSheet("background: #050505; color: #e5e7eb; font-family: Consolas;")
        layout.addWidget(editor, 1)

        buttons = QHBoxLayout()
        approve = QPushButton("Approve")
        deny = QPushButton("Deny")
        approve.setCursor(Qt.CursorShape.PointingHandCursor)
        approve.setToolTip("Approve this action")
        approve.setStyleSheet("color: #10b981;")
        deny.setCursor(Qt.CursorShape.PointingHandCursor)
        deny.setToolTip("Deny this action")
        deny.setStyleSheet("color: #fb7185;")
        buttons.addWidget(approve)
        buttons.addWidget(deny)
        layout.addLayout(buttons)

        approve.clicked.connect(dialog.accept)
        deny.clicked.connect(dialog.reject)
        accepted = dialog.exec() == QDialog.Accepted
        request["approved"] = accepted
        request["edited_content"] = editor.toPlainText() if accepted else str(request.get("content") or "")
        request["event"].set()
