import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from agent_loop import GeminiAgentLoop
from mcp_gateway import GatewayResult
from tool_protocol import ToolCall


class FakeBrowser:
    def __init__(self):
        self.sent = []

    def send_prompt(self, prompt, files, callback=None):
        self.sent.append((prompt, files))
        if callback:
            callback({"ok": True, "method": "fake"})


class FakeGateway:
    def __init__(self, data_dir):
        self.data_dir = Path(data_dir)
        self.calls = []

    def execute(self, call):
        self.calls.append(call)
        return GatewayResult(True, "[OK] fake write")


class FakeHistory:
    def __init__(self):
        self.active_session_id = "test-session"
        self.events = []

    def ensure_session(self, session_mode="persistent_thread"):
        return self.active_session_id

    def add_event(self, role, payload):
        self.events.append((role, payload))


class AgentLoopPromptTests(unittest.TestCase):
    def test_normal_user_prompt_is_not_wrapped_with_tool_instructions(self):
        loop = GeminiAgentLoop.__new__(GeminiAgentLoop)

        prompt = GeminiAgentLoop._build_user_prompt(loop, "create a website about linux", [])

        self.assertEqual(prompt, "create a website about linux")
        self.assertNotIn("GEMINI LOCAL AGENT", prompt)
        self.assertNotIn("USER REQUEST:", prompt)
        self.assertNotIn("LOCAL_AGENT_TURN_ID", prompt)

    def test_extracts_html_artifact_from_plain_gemini_code_reply(self):
        reply = """Here is a template. Save it as index.html.

```html
<!DOCTYPE html>
<html>
<body>Linux</body>
</html>
```
"""

        artifact = GeminiAgentLoop._extract_code_artifact("create a website about linux", reply)

        self.assertIsNotNone(artifact)
        filename, code = artifact
        self.assertEqual(filename, "index.html")
        self.assertIn("<body>Linux</body>", code)

    def test_does_not_extract_artifact_for_non_creation_chat(self):
        reply = """```html
<p>example</p>
```"""

        artifact = GeminiAgentLoop._extract_code_artifact("explain html", reply)

        self.assertIsNone(artifact)

    def test_extracts_plain_text_file_request_when_gemini_uses_txt_artifact(self):
        reply = """Your TXT file is ready

greeting
TXT

I have created the text file with your requested message.
"""

        artifact = GeminiAgentLoop._extract_plain_file_artifact(
            "create a text file please type inside it hi am gemeni",
            reply,
        )

        self.assertIsNotNone(artifact)
        filename, content = artifact
        self.assertEqual(filename, "greeting.txt")
        self.assertEqual(content, "hi am gemeni\n")

    def test_plain_text_file_defaults_to_note_name_without_card_title(self):
        artifact = GeminiAgentLoop._extract_plain_file_artifact(
            "make a txt file saying hello from gemini",
            "Done.",
        )

        self.assertIsNotNone(artifact)
        filename, content = artifact
        self.assertEqual(filename, "note.txt")
        self.assertEqual(content, "hello from gemini\n")

    def test_plain_text_file_supports_say_content_hint(self):
        artifact = GeminiAgentLoop._extract_plain_file_artifact(
            "create a text file say hello am gemeni",
            "Your text file is ready.\n\nhello\nTXT\n",
        )

        self.assertIsNotNone(artifact)
        filename, content = artifact
        self.assertEqual(filename, "hello.txt")
        self.assertEqual(content, "hello am gemeni\n")

    def test_local_file_request_requires_tool_enforcement(self):
        self.assertTrue(GeminiAgentLoop._requires_local_tool("create a text file and write hi inside it"))
        self.assertTrue(GeminiAgentLoop._requires_local_tool("list files on C:\\Users\\awoen\\Desktop"))
        self.assertTrue(GeminiAgentLoop._requires_local_tool("run Get-Process in powershell"))

    def test_plain_question_does_not_require_tool_enforcement(self):
        self.assertFalse(GeminiAgentLoop._requires_local_tool("what is linux"))
        self.assertFalse(GeminiAgentLoop._requires_local_tool("explain how DNS works"))

    def test_tool_enforcement_prompt_never_teaches_placeholder_tool(self):
        loop = GeminiAgentLoop.__new__(GeminiAgentLoop)
        loop.config = {
            "security": {"terminal_cwd": "E:\\AI_Suite"},
            "tools": {"enabled": ["write", "mkdir", "read", "explorer", "powershell"]},
        }
        loop.current_user_text = "create a text file on my Desktop saying hi"

        prompt = loop._tool_enforcement_prompt()

        self.assertNotIn("~@tool@~", prompt)
        self.assertNotIn("~@tool_name@~", prompt)
        self.assertIn("Do NOT use the word 'tool'", prompt)
        self.assertIn("~@write@~", prompt)
        self.assertIn("~@mkdir@~", prompt)

    def test_invalid_tool_calls_catch_placeholder_and_disabled_names(self):
        loop = GeminiAgentLoop.__new__(GeminiAgentLoop)
        loop.config = {"tools": {"enabled": ["write", "read"]}}
        calls = [
            ToolCall("tool", "payload", "~@tool@~", 0),
            ToolCall("write", "file.txt\nhi", "~@write@~", 1),
            ToolCall("browser_open", "https://example.com", "~@browser_open@~", 2),
        ]

        invalid = loop._invalid_tool_calls(calls)

        self.assertEqual([call.name for call in invalid], ["tool", "browser_open"])

    def test_artifact_recovery_runs_before_tool_enforcement(self):
        with TemporaryDirectory() as tmp:
            browser = FakeBrowser()
            gateway = FakeGateway(Path(tmp))
            history = FakeHistory()
            loop = GeminiAgentLoop(
                {
                    "agent": {
                        "force_tool_calls_for_local_actions": True,
                        "max_tool_enforcement_retries": 2,
                        "default_output_dir": str(Path(tmp) / "Desktop"),
                    },
                    "security": {"terminal_cwd": str(Path(tmp))},
                    "tools": {"enabled": ["write"]},
                },
                browser,
                gateway,
                history,
                object(),
            )
            finals = []
            loop.final_signal.connect(finals.append)
            loop.running = True
            loop.current_user_text = "create a text file say hello am gemeni"

            loop._process_reply("Your text file is ready.\n\nhello\nTXT\n")

            self.assertEqual(len(gateway.calls), 1)
            self.assertEqual(gateway.calls[0].name, "write")
            self.assertIn(str(Path(tmp) / "Desktop" / "hello.txt"), gateway.calls[0].raw)
            self.assertIn("hello am gemeni", gateway.calls[0].raw)
            self.assertEqual(browser.sent, [])
            self.assertFalse(loop.running)
            self.assertTrue(finals)

    def test_plain_text_artifact_recovery_defaults_to_desktop(self):
        loop = GeminiAgentLoop.__new__(GeminiAgentLoop)
        loop.config = {"agent": {"default_output_dir": "C:\\Users\\awoen\\Desktop"}}
        loop.current_user_text = "create a text file say hello"

        target_dir = loop._artifact_recovery_base_dir("hello.txt")

        self.assertEqual(str(target_dir), "C:\\Users\\awoen\\Desktop")

    def test_code_artifact_recovery_stays_in_session_artifacts(self):
        with TemporaryDirectory() as tmp:
            loop = GeminiAgentLoop.__new__(GeminiAgentLoop)
            loop.config = {"agent": {"default_output_dir": "C:\\Users\\awoen\\Desktop"}}
            loop.current_user_text = "create a website about linux"
            loop.gateway = FakeGateway(Path(tmp))
            loop.history = FakeHistory()

            target_dir = loop._artifact_recovery_base_dir("index.html")

            self.assertEqual(str(target_dir), "C:\\Users\\awoen\\Desktop")

    def test_synthetic_html_artifact_from_gemini_prose(self):
        reply = """Gemini said

Building a website specifically for System Administrators requires a clean frontend.

1. Technical Stack Selection
Use NGINX, FastAPI, PostgreSQL, and a CLI-inspired frontend for Linux and Windows admins.

2. Key Features for Sysadmins
Include a searchable PowerShell and Bash cheat sheet, subnet calculator, YAML validator, and live monitoring dashboard.

3. Security Hardening
Use TLS, SSH keys, firewall rules, Fail2Ban, and least-privilege access.
"""

        artifact = GeminiAgentLoop._extract_synthetic_html_artifact(
            "create a website for linux and windows system admins",
            reply,
        )

        self.assertIsNotNone(artifact)
        filename, content = artifact
        self.assertEqual(filename, "index.html")
        self.assertIn("<!DOCTYPE html>", content)
        self.assertIn("Linux and Windows System Admins", content)
        self.assertIn("PowerShell", content)
        self.assertIn("</html>", content)

    def test_read_only_powershell_is_mismatched_for_create_file_request(self):
        loop = GeminiAgentLoop.__new__(GeminiAgentLoop)
        loop.current_user_text = "create a text file say hello"

        mismatched = loop._mismatched_tool_calls(
            [ToolCall("powershell", "Get-ChildItem E:\\AI_Suite", "~@powershell@~", 0)]
        )

        self.assertEqual([call.name for call in mismatched], ["powershell"])

    def test_mutating_powershell_is_allowed_for_create_file_request(self):
        loop = GeminiAgentLoop.__new__(GeminiAgentLoop)
        loop.current_user_text = "create a text file say hello"

        mismatched = loop._mismatched_tool_calls(
            [ToolCall("powershell", "Set-Content E:\\AI_Suite\\hello.txt hello", "~@powershell@~", 0)]
        )

        self.assertEqual(mismatched, [])

    def test_malformed_write_payload_catches_copied_instruction_as_path(self):
        loop = GeminiAgentLoop.__new__(GeminiAgentLoop)

        malformed = loop._malformed_payload_tool_calls(
            [
                ToolCall(
                    "write",
                    "tool block. No prose and no Gemini artifact card.\n\nRequired format:\n~@write@~\nE:\\AI_Suite\\x.txt\nrequested file content here",
                    "~@write@~",
                    0,
                )
            ]
        )

        self.assertEqual([call.name for call in malformed], ["write"])

    def test_malformed_write_payload_catches_placeholder_body(self):
        loop = GeminiAgentLoop.__new__(GeminiAgentLoop)

        malformed = loop._malformed_payload_tool_calls(
            [
                ToolCall(
                    "write",
                    "C:\\Users\\awoen\\Desktop\\index.html\nGenerate the complete requested file content on these remaining payload lines.",
                    "~@write@~",
                    0,
                )
            ]
        )

        self.assertEqual([call.name for call in malformed], ["write"])

    def test_valid_write_payload_is_not_malformed(self):
        loop = GeminiAgentLoop.__new__(GeminiAgentLoop)

        malformed = loop._malformed_payload_tool_calls(
            [ToolCall("write", "C:\\Users\\awoen\\Desktop\\note.txt\nhello", "~@write@~", 0)]
        )

        self.assertEqual(malformed, [])

    def test_tool_corrections_do_not_include_placeholder_file_body(self):
        loop = GeminiAgentLoop.__new__(GeminiAgentLoop)
        loop.config = {
            "agent": {"max_tool_enforcement_retries": 2, "default_output_dir": "C:\\Users\\awoen\\Desktop"},
            "tools": {"enabled": ["write", "powershell"]},
        }
        loop.current_user_text = "create a website for linux and windows system admins"
        loop.tool_enforcement_count = 0
        loop.current_loop_count = 0
        loop.running = False
        loop.log_signal = type("Signal", (), {"emit": lambda self, value: None})()
        loop.status_signal = type("Signal", (), {"emit": lambda self, value: None})()
        loop.browser = FakeBrowser()

        loop._correct_mismatched_tool_call([ToolCall("powershell", "Get-ChildItem E:\\AI_Suite", "~@powershell@~", 0)])

        self.assertEqual(len(loop.browser.sent), 1)
        prompt = loop.browser.sent[0][0]
        self.assertNotIn("Generate the complete requested file content", prompt)
        self.assertIn("Use this exact target path", prompt)

    def test_enforcement_prompt_recommends_write_for_text_file_requests(self):
        loop = GeminiAgentLoop.__new__(GeminiAgentLoop)
        loop.config = {
            "security": {"terminal_cwd": "E:\\AI_Suite"},
            "tools": {"enabled": ["write", "mkdir", "read", "explorer", "powershell"]},
        }
        loop.current_user_text = "create a text file say hello"

        prompt = loop._tool_enforcement_prompt()

        self.assertIn("Recommended first tool for this request: use ~@write@~", prompt)


if __name__ == "__main__":
    unittest.main()
