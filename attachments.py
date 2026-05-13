from __future__ import annotations

import os
import shutil
import uuid
from dataclasses import dataclass, asdict
from pathlib import Path


IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".webp", ".bmp", ".gif"}


@dataclass(frozen=True)
class Attachment:
    original_path: str
    stored_path: str
    name: str
    kind: str
    size: int

    def to_dict(self) -> dict:
        return asdict(self)


class AttachmentManager:
    def __init__(self, base_dir: str | Path):
        self.base_dir = Path(base_dir)
        self.base_dir.mkdir(parents=True, exist_ok=True)

    def add_files(self, paths: list[str], session_id: str) -> list[Attachment]:
        session_dir = self.base_dir / session_id
        session_dir.mkdir(parents=True, exist_ok=True)
        attachments: list[Attachment] = []
        for raw_path in paths:
            source = Path(str(raw_path or "").strip().strip('"'))
            if not source.exists() or not source.is_file():
                continue
            suffix = source.suffix
            stored_name = f"{uuid.uuid4().hex}{suffix}"
            target = session_dir / stored_name
            shutil.copy2(source, target)
            kind = "image" if suffix.lower() in IMAGE_EXTENSIONS else "file"
            attachments.append(
                Attachment(
                    original_path=str(source),
                    stored_path=str(target),
                    name=source.name,
                    kind=kind,
                    size=os.path.getsize(target),
                )
            )
        return attachments

    @staticmethod
    def prompt_summary(attachments: list[Attachment]) -> str:
        if not attachments:
            return ""
        lines = ["", "Attached files were uploaded or queued for Gemini:"]
        for item in attachments:
            lines.append(f"- {item.name} ({item.kind}, {item.size} bytes)")
        return "\n".join(lines)
