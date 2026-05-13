import unittest

from tool_protocol import build_tool_result_message, parse_reply, parse_replace_payload, render_tool_contract
from gemini_browser import GeminiBrowser


class ToolProtocolTests(unittest.TestCase):
    def test_parse_single_tool(self):
        parsed = parse_reply("Please wait\n~@read@~ E:\\AI_Suite\\x.txt ~@exit@~")
        self.assertEqual(len(parsed.tool_calls), 1)
        self.assertEqual(parsed.tool_calls[0].name, "read")
        self.assertIn("Please wait", parsed.chat_text)

    def test_parse_multiple_tools(self):
        parsed = parse_reply("~@mcp_status@~ check ~@exit@~\n~@memory_get@~ project ~@exit@~")
        self.assertEqual([call.name for call in parsed.tool_calls], ["mcp_status", "memory_get"])

    def test_common_tool_alias(self):
        parsed = parse_reply("~@mkdri@~ E:\\AI_Suite\\Demo ~@exit@~")
        self.assertEqual(len(parsed.tool_calls), 1)
        self.assertEqual(parsed.tool_calls[0].name, "mkdir")

    def test_read_file_alias(self):
        parsed = parse_reply("~@read_file@~ C:\\Users\\awoen\\Desktop\\notes.txt ~@exit@~")
        self.assertEqual(len(parsed.tool_calls), 1)
        self.assertEqual(parsed.tool_calls[0].name, "read")

    def test_replace_payload(self):
        path, old, new = parse_replace_payload("E:\\x.txt\n---OLD---\na\n---NEW---\nb")
        self.assertEqual(path, "E:\\x.txt")
        self.assertEqual(old, "a")
        self.assertEqual(new, "b")

    def test_tool_result_message(self):
        parsed = parse_reply("~@read@~ file.txt ~@exit@~")
        msg = build_tool_result_message("abc", [(parsed.tool_calls[0], "hello")])
        self.assertIn("TOOL RESULTS FOR TURN abc", msg)
        self.assertIn("[TOOL_RESULT:read:0]", msg)

    def test_contract_tells_gemini_to_use_tools_for_local_actions(self):
        contract = render_tool_contract(
            access_mode="full",
            approval_policy="guarded",
            roots=["E:\\AI_Suite"],
            enabled_tools=["mkdir", "explorer"],
        )
        lowered = contract.lower()
        self.assertIn("you do have access", lowered)
        self.assertIn("do not answer local-action requests with manual instructions", lowered)
        self.assertIn("~@mkdir@~", contract)
        self.assertIn("E:\\AI_Suite", contract)

    def test_latest_sigil_block_prefers_visible_reply_after_contract_examples(self):
        page_text = (
            "Example: ~@mkdir@~ E:\\AI_Suite\\Example ~@exit@~\n"
            "Gemini reply:\n"
            "~@mkdir@~ C:\\Users\\awoen\\Desktop\\hello gemeni ~@exit@~"
        )
        latest = GeminiBrowser._latest_sigil_block(page_text)
        self.assertEqual(latest, "~@mkdir@~ C:\\Users\\awoen\\Desktop\\hello gemeni ~@exit@~")

    def test_send_script_does_not_click_generic_submit_or_stop_controls(self):
        script = GeminiBrowser._send_text_script("hello")

        self.assertNotIn('button[type="submit"]', script)
        self.assertIn("stop|cancel|pause|interrupt|generating", script)
        self.assertIn("button[aria-label*=\"Send\"]", script)

    def test_read_state_script_ignores_hidden_unified_composer_controls(self):
        script = GeminiBrowser._read_state_script()

        self.assertIn("insideHiddenComposer", script)
        self.assertIn("data-gla-native-composer-root", script)
        self.assertIn("style.opacity !== '0'", script)

    def test_model_mode_script_targets_normal_gemini_mode_controls(self):
        script = GeminiBrowser._activate_model_mode_script(["pro"])

        self.assertIn("model mode opener", script)
        self.assertIn("'fast', 'pro', 'thinking'", script)
        self.assertIn("subscription|business|about gemini", script)
        self.assertIn("model-menu", script)


if __name__ == "__main__":
    unittest.main()
