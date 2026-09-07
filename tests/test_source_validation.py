"""Source validation is independent of model metadata and Python execution."""

import unittest
from unittest.mock import AsyncMock, patch

from ai_dungeon_crawl.contracts import AgentTurn, GameObservation, validate_python_source
from ai_dungeon_crawl.policies import PythonScript
from ai_dungeon_crawl.repl import PythonRepl


class SourceValidationTests(unittest.TestCase):
    def test_accepts_empty_source_and_does_not_check_syntax(self):
        for code in ("", "pass", "for", "x" * 65536, "é" * 32768):
            with self.subTest(code_length=len(code)):
                self.assertIsNone(validate_python_source(code))

    def test_rejects_non_text_and_oversized_utf8(self):
        for code in (None, 123, b"pass", "x" * 65537, "é" * 32769):
            with self.subTest(type=type(code), length=len(code) if isinstance(code, str) else None):
                with self.assertRaisesRegex(ValueError, "Code must be text of at most 64 KiB"):
                    validate_python_source(code)

    def test_data_models_enforce_the_same_byte_limit(self):
        for model in (AgentTurn, PythonScript):
            with self.subTest(model=model):
                self.assertEqual(model(code="é" * 32768).code, "é" * 32768)
                with self.assertRaises(ValueError):
                    model(code="é" * 32769)


class ReplValidationTests(unittest.IsolatedAsyncioTestCase):
    async def test_invalid_source_is_rejected_before_starting_worker(self):
        repl = PythonRepl()
        press = AsyncMock()
        with patch.object(repl, "_start", new_callable=AsyncMock) as start:
            with self.assertRaises(ValueError):
                await repl.execute_python("é" * 32769, GameObservation(0, "screen"), press)
            start.assert_not_awaited()
            press.assert_not_awaited()
        await repl.close()
