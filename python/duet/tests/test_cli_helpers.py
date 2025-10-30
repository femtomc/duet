import os
import time
import unittest

from duet.cli import (
    _clean_assistant_message,
    _clean_user_message,
    _extract_keywords,
    _format_timestamp,
    _structured_value_metadata,
    _structured_value_renderable,
)


class CliHelperTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls._original_tz = os.environ.get("TZ")
        os.environ["TZ"] = "UTC"
        if hasattr(time, "tzset"):
            time.tzset()

    @classmethod
    def tearDownClass(cls) -> None:
        if cls._original_tz is None:
            os.environ.pop("TZ", None)
        else:
            os.environ["TZ"] = cls._original_tz
        if hasattr(time, "tzset"):
            time.tzset()

    def test_format_timestamp_with_z_suffix(self) -> None:
        result = _format_timestamp("2025-01-02T03:04:05.123456Z")
        self.assertEqual(result, "2025-01-02 03:04:05 UTC")

    def test_format_timestamp_with_offset(self) -> None:
        result = _format_timestamp("2025-01-02T03:04:05+0200")
        self.assertEqual(result, "2025-01-02 01:04:05 UTC")

    def test_format_timestamp_invalid(self) -> None:
        self.assertEqual(_format_timestamp("not-a-timestamp"), "not-a-timestamp")

    def test_format_timestamp_blank(self) -> None:
        self.assertIsNone(_format_timestamp("  "))

    def test_extract_keywords_prefers_user_content(self) -> None:
        text = (
            "User: Build streaming pipeline for telemetry ingestion and analytics.\n"
            "Assistant: Sure thing!"
        )
        self.assertEqual(
            _extract_keywords([text]),
            ["analytics", "ingestion", "streaming"],
        )

    def test_extract_keywords_ignores_stopwords(self) -> None:
        text = "User: Please help me triage authentication errors and retries quickly."
        keywords = _extract_keywords([text])
        self.assertIn("authentication", keywords)
        self.assertNotIn("please", keywords)
        self.assertNotIn("help", keywords)
        self.assertNotIn("user", keywords)

    def test_clean_user_message_returns_latest(self) -> None:
        text = (
            "User: First request here.\n"
            "Assistant: Noted.\n"
            "User: Second question please with   extra   spacing."
        )
        self.assertEqual(
            _clean_user_message(text),
            "Second question please with extra spacing.",
        )

    def test_clean_assistant_message_returns_latest(self) -> None:
        text = (
            "Assistant: Initial response.\n"
            "User: follow up.\n"
            "Assistant: Final answer with   spacing.\n"
        )
        self.assertEqual(
            _clean_assistant_message(text),
            "Final answer with spacing.",
        )

    def test_clean_helpers_handle_missing_content(self) -> None:
        self.assertIsNone(_clean_user_message(None))
        self.assertIsNone(_clean_assistant_message(None))

    def test_structured_value_metadata_record(self) -> None:
        structured = {
            "type": "record",
            "label": "agent-response",
            "field_count": 3,
            "summary": "agent-response#3",
        }
        metadata = _structured_value_metadata(structured)
        self.assertIn(("Type", "record"), metadata)
        self.assertIn(("Label", "agent-response"), metadata)
        self.assertIn(("Fields", "3"), metadata)

    def test_structured_value_renderable_returns_json(self) -> None:
        structured = {"type": "string", "value": "hello", "summary": "hello"}
        renderable = _structured_value_renderable(structured)
        self.assertIsNotNone(renderable)


if __name__ == "__main__":
    unittest.main()
