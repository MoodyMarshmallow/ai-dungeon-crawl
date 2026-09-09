import asyncio
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from unittest.mock import AsyncMock, Mock

from ai_dungeon_crawl.contracts import GameAction
from ai_dungeon_crawl.game.dcss import DCSSGameSession, key_bytes
from ai_dungeon_crawl.game.terminal import BoundaryDecoder, TerminalScreen


class TerminalTests(unittest.TestCase):
    def test_fragmented_markers_snapshot_before_following_output(self):
        terminal = TerminalScreen(80, 24)
        observations = []
        marker = b"\x1b]777;dcss-input;nonce\x07"
        decoder = BoundaryDecoder(marker, terminal,
                                  lambda: observations.append(terminal.observation(len(observations))))
        stream = b"\x1b[31;1mred" + marker + b"\x1b[0m\rNEW" + marker
        for byte in stream:
            decoder.feed(bytes([byte]))
        self.assertEqual(len(observations), 2)
        self.assertTrue(observations[0].screen.startswith("red"))
        self.assertTrue(observations[1].screen.startswith("NEW"))
        self.assertEqual(observations[0].styles[0].fg, "red")
        self.assertTrue(observations[0].styles[0].bold)
        self.assertEqual(observations[1].styles, ())
        self.assertNotIn("dcss-input", observations[1].screen)

    def test_clear_cursor_utf8_and_visual_attributes(self):
        terminal = TerminalScreen(80, 24)
        terminal.feed(b"old text\x1b[2J\x1b[3;5H\x1b[3;4;5;7;44m")
        for byte in "é界".encode():
            terminal.feed(bytes([byte]))
        observation = terminal.observation(1)
        self.assertNotIn("old text", observation.screen)
        self.assertEqual(observation.screen.splitlines()[2][4:6], "é界")
        style = next(run for run in observation.styles if run.row == 2 and run.col == 4)
        self.assertEqual(style.bg, "blue")
        self.assertTrue(all((style.italics, style.underline, style.blink, style.reverse)))
        self.assertEqual(observation.cursor, (2, 7))
        terminal.feed(b"\x1b[?25l")
        self.assertIsNone(terminal.observation(2).cursor)

    def test_whitespace_and_dimensions_survive(self):
        terminal = TerminalScreen(80, 24)
        terminal.feed(b"a\x1b[24;80HZ")
        observation = terminal.observation(1)
        rows = observation.screen.split("\n")
        self.assertEqual(len(rows), 24)
        self.assertTrue(all(len(row) == 80 for row in rows))
        self.assertEqual(rows[0], "a" + " " * 79)
        self.assertEqual(rows[-1][-1], "Z")
        self.assertEqual(observation.cursor, (23, 79))

    def test_key_mapping_sends_exactly_one_key(self):
        self.assertEqual(key_bytes(GameAction("UP")), b"\x1bOA")
        self.assertEqual(key_bytes(GameAction("ESC")), b"\x1b")
        self.assertEqual(key_bytes(GameAction("CTRL+P")), b"\x10")
        self.assertEqual(key_bytes(GameAction("é")), "é".encode())


class TransportFailureTests(unittest.IsolatedAsyncioTestCase):
    async def test_startup_defaults_leave_weapon_choice_and_enable_exit_guard(self):
        with tempfile.TemporaryDirectory() as directory:
            overrides = ("-name", "TransportTest", "-background", "Fighter")
            session = DCSSGameSession("/no/game", save_dir=directory, extra_args=overrides)
            with patch("ai_dungeon_crawl.game.dcss.socket.socket.bind"), patch(
                "ai_dungeon_crawl.game.dcss.asyncio.create_subprocess_exec",
                new_callable=AsyncMock, side_effect=FileNotFoundError,
            ) as spawn:
                try:
                    with self.assertRaises(FileNotFoundError):
                        await session.start()
                finally:
                    await session.close()
            args = spawn.call_args.args
            self.assertEqual(args.count("-name"), 1)
            self.assertEqual(args.count("-background"), 1)
            self.assertEqual(args[args.index("-species") + 1], "Minotaur")
            self.assertFalse(any("weapon" in arg for arg in args))
            self.assertEqual(args[-len(overrides):], overrides)
            self.assertEqual(spawn.call_args.kwargs["env"]["DCSS_HARNESS_NO_EXIT"], "1")

    def test_fragmented_tile_utf8_is_decoded_only_after_full_message(self):
        messages = []
        session = DCSSGameSession("/no/game", on_tiles=lambda batch: messages.extend(batch))
        raw = json.dumps({"msg": "test", "text": "é"}, ensure_ascii=False).encode() + b"\n"
        split = raw.index("é".encode()) + 1
        session._socket = Mock()
        session._socket.recv.side_effect = [raw[:split], BlockingIOError()]
        session._read_tiles()
        self.assertEqual(messages, [])
        session._socket.recv.side_effect = [raw[split:], b'*{"msg":"flush_messages"}\n', BlockingIOError()]
        session._read_tiles()
        self.assertEqual(messages, [{"msg": "test", "text": "é"}])

    async def test_timeout_poisoning_prevents_action_retry(self):
        session = DCSSGameSession("/no/game", readiness_timeout=0.01)
        session._started = True
        session._master = 999
        with patch("ai_dungeon_crawl.game.dcss.os.write", return_value=1) as write:
            with self.assertRaisesRegex(TimeoutError, "never retried"):
                await session.step(GameAction("l"))
            with self.assertRaisesRegex(RuntimeError, "not accepting"):
                await session.step(GameAction("l"))
        self.assertEqual(write.call_count, 1)
        session._master = None
        await session.close()
        await session.close()

    async def test_close_after_missing_executable_preserves_run_directory(self):
        with tempfile.TemporaryDirectory() as directory:
            session = DCSSGameSession("/no/game", save_dir=directory)
            # Failure before sockets can be used still leaves all cleanup paths valid.
            try:
                with self.assertRaises((FileNotFoundError, PermissionError)):
                    await session.start()
            finally:
                await session.close()
                await session.close()
            self.assertTrue(Path(directory).exists())

    def test_bounds(self):
        for kwargs in ({"readiness_timeout": float("nan")}, {"readiness_timeout": 0},
                       {"width": 79}, {"height": 10000}):
            with self.assertRaises(ValueError):
                DCSSGameSession("/no/game", **kwargs)


@unittest.skipUnless(os.environ.get("DCSS_TEST_BINARY"), "Set DCSS_TEST_BINARY for real-game tests")
class RealDCSSTests(unittest.IsolatedAsyncioTestCase):
    async def test_ignored_input_has_new_boundary_and_clean_shutdown(self):
        messages = []
        with tempfile.TemporaryDirectory(prefix="dcss-test-") as directory:
            session = DCSSGameSession(os.environ["DCSS_TEST_BINARY"], save_dir=directory,
                                      on_tiles=lambda batch: messages.extend(batch))
            try:
                before = await session.start()
                self.assertIn("Agent the Minotaur Berserker", before.screen)
                self.assertIn("choice of weapons", before.screen)
                self.assertNotIn("Health:", before.screen)
                after = await session.step(GameAction("!"))
                self.assertEqual(before.screen, after.screen)
                self.assertGreater(after.id, before.id)
                self.assertEqual((after.width, after.height), (100, 30))
                self.assertTrue(after.styles)
                self.assertIn("version", [message["msg"] for message in messages])
                for key in ("ESC", "CTRL+G", " ", "X", "CTRL+Q", "CTRL+C"):
                    guarded = await session.step(GameAction(key))
                    self.assertEqual(before.screen, guarded.screen, key)
                    self.assertFalse(guarded.ended, key)
                    self.assertIsNone(session._process.returncode, key)
            finally:
                await session.close()
                await session.close()
            self.assertIsNotNone(session._process.returncode)
            self.assertTrue(Path(directory).exists())

    async def test_character_override_inventory_wait_and_blocked_exit(self):
        messages = []
        with tempfile.TemporaryDirectory(prefix="dcss-test-") as directory:
            session = DCSSGameSession(os.environ["DCSS_TEST_BINARY"], save_dir=directory,
                extra_args=("-name", "TransportTest", "-species", "Minotaur", "-background", "Fighter"),
                on_tiles=lambda batch: messages.extend(batch))
            try:
                first = await session.start()
                self.assertIn("TransportTest the Minotaur Fighter", first.screen)
                self.assertIn("choice of weapons", first.screen)
                playing = await session.step(GameAction("a"))
                self.assertIn("Health:", playing.screen)
                self.assertIn("map", [message["msg"] for message in messages])
                inventory = await session.step(GameAction("i"))
                self.assertIn("rapier", inventory.screen)
                self.assertNotIn("Health:", inventory.screen)
                await session.step(GameAction("ESC"))
                waited = await session.step(GameAction("."))
                self.assertIn("Time: 1.0", waited.screen)
                for key in ("S", "CTRL+S", "CTRL+Q", "CTRL+Z"):
                    blocked = await session.step(GameAction(key))
                    self.assertIn("Session exit is disabled by the harness.", blocked.screen)
                    self.assertIn("Health:", blocked.screen)
                    self.assertFalse(blocked.ended)
                    self.assertIsNone(session._process.returncode)
                for key in ("S", "Q"):
                    await session.step(GameAction("ESC"))
                    blocked = await session.step(GameAction(key))
                    self.assertIn("Session exit is disabled by the harness.", blocked.screen)
                    self.assertIn("Health:", blocked.screen)
                    self.assertFalse(blocked.ended)
                # The guard acts on game commands, not raw keys: S still works
                # as ordinary text when entering an item inscription.
                await session.step(GameAction("{"))
                await session.step(GameAction("a"))
                await session.step(GameAction("S"))
                await session.step(GameAction("ENTER"))
                inscribed = await session.step(GameAction("i"))
                self.assertIn("{S}", inscribed.screen)
            finally:
                await session.close()
