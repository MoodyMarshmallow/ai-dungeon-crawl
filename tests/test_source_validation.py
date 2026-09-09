import unittest
from unittest.mock import AsyncMock, patch

from ai_dungeon_crawl.contracts import AgentTurn, GameObservation, validate_source
from ai_dungeon_crawl.agent.policies import ShellScript
from ai_dungeon_crawl.shell.terminal import ShellTerminal


class SourceValidationTests(unittest.TestCase):
    def test_timeout_bounds_and_default(self):
        for model in (AgentTurn, ShellScript):
            self.assertIsNone(model(code=":").timeout_ms)
            for value in (None, 1, 5000, 180000):
                self.assertEqual(model(code=":", timeout_ms=value).timeout_ms, value)
            for value in (0, -1, 180001, True, 1.5, "5000"):
                with self.subTest(model=model, value=value), self.assertRaises(ValueError):
                    model(code=":", timeout_ms=value)

    def test_accepts_empty_source_and_does_not_check_syntax(self):
        for code in ("", "pass", "for", "x" * 65536, "é" * 32768):
            with self.subTest(code_length=len(code)):
                self.assertIsNone(validate_source(code))

    def test_rejects_non_text_and_oversized_utf8(self):
        for code in (None, 123, b"pass", "x" * 65537, "é" * 32769):
            with self.subTest(type=type(code), length=len(code) if isinstance(code, str) else None):
                with self.assertRaisesRegex(ValueError, "Code must be text of at most 64 KiB"):
                    validate_source(code)

    def test_data_models_enforce_the_same_byte_limit(self):
        for model in (AgentTurn, ShellScript):
            with self.subTest(model=model):
                self.assertEqual(model(code="é" * 32768).code, "é" * 32768)
                with self.assertRaises(ValueError):
                    model(code="é" * 32769)


class ShellValidationTests(unittest.IsolatedAsyncioTestCase):
    async def test_invalid_timeout_is_rejected_before_starting_worker(self):
        terminal = ShellTerminal()
        with patch.object(terminal, "_prepare") as prepare:
            for value in (0, 180001, True, "5000"):
                with self.assertRaises(ValueError):
                    await terminal.execute_shell(":", GameObservation(0, ""), None, timeout_ms=value)
            prepare.assert_not_called()
        await terminal.close()

    async def test_invalid_source_is_rejected_before_starting_worker(self):
        terminal = ShellTerminal()
        press = AsyncMock()
        with patch.object(terminal, "_prepare") as prepare:
            with self.assertRaises(ValueError):
                await terminal.execute_shell("é" * 32769, GameObservation(0, "screen"), press)
            prepare.assert_not_called()
            press.assert_not_awaited()
        await terminal.close()
