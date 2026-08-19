import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from model_scheduler import (
    format_model_key,
    format_selector_key,
    parse_model_key,
    parse_selector_key,
)


class SelectorKeyCodecTests(unittest.TestCase):
    """provider/model selector 格式转换 helper 测试。"""

    def test_format_basic(self):
        self.assertEqual(format_selector_key("gpt-4o", "openai"), "openai/gpt-4o")
        self.assertEqual(
            format_selector_key("claude-3-5-sonnet", "anthropic"),
            "anthropic/claude-3-5-sonnet",
        )

    def test_format_provider_empty_returns_bare_model(self):
        self.assertEqual(format_selector_key("gpt-4o", ""), "gpt-4o")
        self.assertEqual(format_selector_key("gpt-4o", None), "gpt-4o")

    def test_format_model_empty_returns_empty(self):
        self.assertEqual(format_selector_key("", "openai"), "")
        self.assertEqual(format_selector_key(None, "openai"), "")

    def test_parse_basic(self):
        self.assertEqual(parse_selector_key("openai/gpt-4o"), ("gpt-4o", "openai"))
        self.assertEqual(
            parse_selector_key("anthropic/claude-3-5-sonnet"),
            ("claude-3-5-sonnet", "anthropic"),
        )

    def test_parse_no_slash_returns_bare_model(self):
        self.assertEqual(parse_selector_key("gpt-4o"), ("gpt-4o", ""))

    def test_parse_empty_input(self):
        self.assertEqual(parse_selector_key(""), ("", ""))
        self.assertEqual(parse_selector_key(None), ("", ""))
        self.assertEqual(parse_selector_key("   "), ("", ""))

    def test_roundtrip_with_provider(self):
        for model, provider in [
            ("gpt-4o", "openai"),
            ("claude-3-5-sonnet", "anthropic"),
            ("gemini-2.0-flash", "google"),
            ("deepseek-chat", "deepseek"),
        ]:
            with self.subTest(model=model, provider=provider):
                value = format_selector_key(model, provider)
                self.assertEqual(parse_selector_key(value), (model, provider))

    def test_roundtrip_provider_empty(self):
        self.assertEqual(
            parse_selector_key(format_selector_key("gpt-4o", "")),
            ("gpt-4o", ""),
        )

    def test_model_name_with_slash_roundtrips_with_provider(self):
        model = "meta-llama/llama-3-8b-instruct"
        value = format_selector_key(model, "openai")
        self.assertEqual(value, "openai/meta-llama/llama-3-8b-instruct")
        self.assertEqual(parse_selector_key(value), (model, "openai"))

    def test_at_prefixed_model_not_misparsed(self):
        self.assertEqual(format_selector_key("@provider:model", ""), "@provider:model")
        self.assertEqual(parse_selector_key("@provider:model"), ("@provider:model", ""))
        self.assertEqual(
            format_selector_key("@provider:model", "openai"),
            "openai/@provider:model",
        )
        self.assertEqual(
            parse_selector_key("openai/@provider:model"),
            ("@provider:model", "openai"),
        )

    def test_selector_codec_does_not_parse_internal_key_format(self):
        self.assertEqual(parse_selector_key("gpt-4o@openai"), ("gpt-4o@openai", ""))
        self.assertEqual(format_selector_key("gpt-4o@openai", ""), "gpt-4o@openai")


if __name__ == "__main__":
    unittest.main()
