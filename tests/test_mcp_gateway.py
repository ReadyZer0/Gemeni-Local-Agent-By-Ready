import tempfile
import unittest
import importlib.util
import http.server
import socketserver
import threading
from pathlib import Path

from mcp_gateway import ToolGateway
from tool_protocol import ToolCall


def test_config(root):
    return {
        "app": {"approval_policy": "full_auto"},
        "security": {
            "access_mode": "restricted",
            "filesystem_roots": [str(root)],
            "terminal_cwd": str(root),
            "terminal_timeout_seconds": 5,
            "max_read_bytes": 10000,
        },
        "tools": {
            "enabled": [
                "read",
                "write",
                "append",
                "mkdir",
                "delete",
                "memory_put",
                "memory_get",
                "mcp_status",
                "excel_write",
                "excel_read",
                "browser_type",
            ],
            "approval_required": ["write", "append", "mkdir", "delete", "excel_write", "browser_type"],
        },
        "mcp_servers": {
            "filesystem": {"enabled": True, "command": "npx"},
            "github": {"enabled": False, "url": "https://api.githubcopilot.com/mcp/"},
        },
    }


def ask_config(root):
    cfg = test_config(root)
    cfg["security"]["access_mode"] = "ask"
    return cfg


class GatewayTests(unittest.TestCase):
    def test_read_allowed_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            target = root / "hello.txt"
            target.write_text("hello", encoding="utf-8")
            gateway = ToolGateway(test_config(root), root)
            result = gateway.execute(ToolCall("read", str(target), "~@read@~", 0))
            self.assertTrue(result.ok)
            self.assertIn("hello", result.text)

    def test_memory_round_trip(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            gateway = ToolGateway(test_config(root), root)
            put = gateway.execute(ToolCall("memory_put", "project\nGemini Local Agent", "~@memory_put@~", 0))
            self.assertTrue(put.ok)
            get = gateway.execute(ToolCall("memory_get", "project", "~@memory_get@~", 0))
            self.assertTrue(get.ok)
            self.assertIn("Gemini Local Agent", get.text)

    def test_file_mutation_tools_in_full_auto_temp_root(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            gateway = ToolGateway(test_config(root), root)
            folder = root / "work"
            mkdir = gateway.execute(ToolCall("mkdir", str(folder), "~@mkdir@~", 0))
            self.assertTrue(mkdir.ok)
            target = folder / "notes.txt"
            write = gateway.execute(ToolCall("write", f"{target}\nhello", "~@write@~", 0))
            self.assertTrue(write.ok)
            append = gateway.execute(ToolCall("append", f"{target}\n world", "~@append@~", 0))
            self.assertTrue(append.ok)
            self.assertEqual(target.read_text(encoding="utf-8"), "hello world")
            delete = gateway.execute(ToolCall("delete", str(target), "~@delete@~", 0))
            self.assertTrue(delete.ok)
            self.assertFalse(target.exists())

    def test_restricted_access_blocks_outside_root(self):
        with tempfile.TemporaryDirectory() as tmp, tempfile.TemporaryDirectory() as other:
            root = Path(tmp)
            outside = Path(other) / "outside.txt"
            outside.write_text("secret", encoding="utf-8")
            gateway = ToolGateway(test_config(root), root)
            result = gateway.execute(ToolCall("read", str(outside), "~@read@~", 0))
            self.assertFalse(result.ok)
            self.assertIn("outside allowed roots", result.text)

    def test_ask_access_can_approve_read_outside_root(self):
        with tempfile.TemporaryDirectory() as tmp, tempfile.TemporaryDirectory() as other:
            root = Path(tmp)
            outside = Path(other) / "outside.txt"
            outside.write_text("approved read", encoding="utf-8")

            def approve(_title, content, _editable, _metadata):
                return True, content

            gateway = ToolGateway(ask_config(root), root, approval_callback=approve)
            result = gateway.execute(ToolCall("read", str(outside), "~@read@~", 0))
            self.assertTrue(result.ok)
            self.assertIn("approved read", result.text)

    def test_mcp_status_reports_github_disabled(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            gateway = ToolGateway(test_config(root), root)
            result = gateway.execute(ToolCall("mcp_status", "check", "~@mcp_status@~", 0))
            self.assertTrue(result.ok)
            self.assertIn("github: disabled", result.text)

    @unittest.skipIf(importlib.util.find_spec("openpyxl") is None, "openpyxl not installed")
    def test_excel_write_and_read(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            gateway = ToolGateway(test_config(root), root)
            workbook = root / "book.xlsx"
            write = gateway.execute(
                ToolCall(
                    "excel_write",
                    f"path: {workbook}\nsheet: Data\ncell: B2\nvalue: hello excel",
                    "~@excel_write@~",
                    0,
                )
            )
            self.assertTrue(write.ok, write.text)
            read = gateway.execute(
                ToolCall(
                    "excel_read",
                    f"path: {workbook}\nsheet: Data\nrange: B2:B2",
                    "~@excel_read@~",
                    0,
                )
            )
            self.assertTrue(read.ok, read.text)
            self.assertIn("hello excel", read.text)

    @unittest.skipIf(importlib.util.find_spec("playwright") is None, "playwright not installed")
    def test_browser_type_against_local_page(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "index.html").write_text("<input id='q' />", encoding="utf-8")
            handler = lambda *args, **kwargs: http.server.SimpleHTTPRequestHandler(*args, directory=str(root), **kwargs)
            with socketserver.TCPServer(("127.0.0.1", 0), handler) as httpd:
                thread = threading.Thread(target=httpd.serve_forever, daemon=True)
                thread.start()
                url = f"http://127.0.0.1:{httpd.server_address[1]}/index.html"
                gateway = ToolGateway(test_config(root), root)
                result = gateway.execute(
                    ToolCall(
                        "browser_type",
                        f"url: {url}\nselector: #q\ntext: hello browser",
                        "~@browser_type@~",
                        0,
                    )
                )
                httpd.shutdown()
            self.assertTrue(result.ok, result.text)
            self.assertIn("ok", result.text.lower())


if __name__ == "__main__":
    unittest.main()
