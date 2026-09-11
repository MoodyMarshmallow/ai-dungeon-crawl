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
            self.assertEqual(spawn.call_args.kwargs["env"]["DCSS_HARNESS_SCORE"], "1")
            self.assertEqual(spawn.call_args.kwargs["env"]["DCSS_HARNESS_OUTCOME"], "1")

    def test_explicit_outcomes_end_at_input_boundary_without_process_exit(self):
        for outcome in ("death", "win", "quit"):
            tiles = []
            session = DCSSGameSession("/no/game", on_tiles=lambda batch: tiles.extend(batch))
            session._socket = Mock()
            session._socket.recv.side_effect = [
                json.dumps({"msg": "harness_outcome", "outcome": outcome}).encode() + b'\n',
                BlockingIOError(),
            ]
            session._boundary()
            observation = session._queue.get_nowait()
            self.assertTrue(observation.ended)
            self.assertEqual(observation.outcome, outcome)
            self.assertEqual(session.outcome, outcome)
            self.assertEqual(tiles, [])

    def test_death_text_and_final_score_are_not_death_signals(self):
        session = DCSSGameSession("/no/game")
        session._terminal.feed(b"You die... Goodbye, Agent.")
        session._socket = Mock()
        session._socket.recv.side_effect = [
            b'{"msg":"harness_score","score":123,"game_turn":7,"final":true,"game_time":70}\n',
            b'{"msg":"harness_score","score":null,"game_turn":7,"final":false,"game_time":null}\n',
            BlockingIOError(),
        ]
        session._boundary()
        observation = session._queue.get_nowait()
        self.assertFalse(observation.ended)
        self.assertIsNone(observation.outcome)

    async def test_process_exit_without_outcome_is_not_death(self):
        session = DCSSGameSession("/no/game")
        session._process = Mock(wait=AsyncMock(return_value=0))
        await session._watch_exit()
        observation = session._queue.get_nowait()
        self.assertTrue(observation.ended)
        self.assertIsNone(observation.outcome)

    async def test_invalid_and_conflicting_outcomes_fail_transport(self):
        for payload in ({"msg": "harness_outcome"},
                        {"msg": "harness_outcome", "outcome": "timeout"},
                        {"msg": "harness_outcome", "outcome": "win"}):
            session = DCSSGameSession("/no/game")
            session.outcome = "death"
            session._socket = Mock()
            session._socket.recv.side_effect = [json.dumps(payload).encode() + b'\n', BlockingIOError()]
            with patch("asyncio.get_running_loop") as get_loop:
                get_loop.return_value.remove_reader = Mock()
                session._read_tiles()
            self.assertTrue(session._failed)
            self.assertIsInstance(session._queue.get_nowait(), ValueError)

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

    def test_score_messages_are_delivered_separately_from_tiles(self):
        tiles = []
        scores = []
        session = DCSSGameSession(
            "/no/game", on_tiles=lambda batch: tiles.extend(batch),
            on_score=lambda score, game_turn, final, game_time: scores.append(
                (score, game_turn, final, game_time)))
        session._socket = Mock()
        session._socket.recv.side_effect = [
            b'{"msg":"map","cells":[]}\n'
            b'{"msg":"harness_score","score":null,"game_turn":0,"final":false,"game_time":null}\n'
            b'{"msg":"harness_score","score":123,"game_turn":7,"final":true,"game_time":70}\n',
            b'{"msg":"harness_score","score":null,"game_turn":7,"final":false,"game_time":null}\n',
            BlockingIOError(),
        ]
        session._read_tiles()
        self.assertEqual(tiles, [{"msg": "map", "cells": []}])
        self.assertEqual(scores, [(None, 0, False, None), (123, 7, True, 70)])

    async def test_invalid_score_payload_is_reported(self):
        payloads = (
            b'{"msg":"harness_score","game_turn":0,"final":false,"game_time":null}\n',
            b'{"msg":"harness_score","score":true,"game_turn":0,"final":false,"game_time":null}\n',
            b'{"msg":"harness_score","score":null,"game_turn":0,"final":true,"game_time":70}\n',
        )
        for payload in payloads:
            session = DCSSGameSession("/no/game")
            session._socket = Mock()
            session._socket.recv.side_effect = [payload, BlockingIOError()]
            with patch("asyncio.get_running_loop") as get_loop:
                get_loop.return_value.remove_reader = Mock()
                session._read_tiles()
            self.assertTrue(session._failed)
            self.assertIsInstance(session._queue.get_nowait(), ValueError)

    async def test_score_callback_failure_is_reported(self):
        failure = RuntimeError("recorder failed")
        session = DCSSGameSession(
            "/no/game", on_score=Mock(side_effect=failure))
        session._socket = Mock()
        session._socket.recv.side_effect = [
            b'{"msg":"harness_score","score":1,"game_turn":1,"final":false,"game_time":null}\n',
            BlockingIOError(),
        ]
        with patch("asyncio.get_running_loop") as get_loop:
            get_loop.return_value.remove_reader = Mock()
            session._read_tiles()
        self.assertTrue(session._failed)
        self.assertIs(session._queue.get_nowait(), failure)

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
    async def test_native_quit_is_not_death(self):
        spawn = asyncio.create_subprocess_exec

        async def allow_exit_for_test(*args, **kwargs):
            kwargs["env"].pop("DCSS_HARNESS_NO_EXIT")
            return await spawn(*args, **kwargs)

        with tempfile.TemporaryDirectory(prefix="dcss-quit-test-") as directory:
            session = DCSSGameSession(os.environ["DCSS_TEST_BINARY"], save_dir=directory)
            try:
                with patch("ai_dungeon_crawl.game.dcss.asyncio.create_subprocess_exec",
                           side_effect=allow_exit_for_test):
                    await session.start()
                await session.step(GameAction("a"))
                confirmation = await session.step(GameAction("CTRL+Q"))
                self.assertIn("abandon this character", confirmation.screen)
                self.assertIsNone(confirmation.outcome)
                for key in (*"quit", "ENTER"):
                    observation = await session.step(GameAction(key))
                self.assertTrue(observation.ended)
                self.assertEqual(observation.outcome, "quit")
                self.assertIsNone(session._process.returncode)
            finally:
                await session.close()

    async def test_death_and_final_score_arrive_before_postmortem_exit(self):
        scores = []
        with tempfile.TemporaryDirectory(prefix="dcss-death-test-") as directory:
            session = DCSSGameSession(os.environ["DCSS_TEST_BINARY"], save_dir=directory,
                                      on_score=lambda *event: scores.append(event))
            try:
                await session.start()
                # Use native wizard Lua to exercise final death deterministically.
                # Wizard death confirmations must not themselves end the session.
                for key in ("a", "&", *"wiz", "ENTER", "CTRL+T", *"you.die()", "ENTER"):
                    before = await session.step(GameAction(key))
                    self.assertIsNone(before.outcome)
                self.assertIn("Die?", before.screen)
                dead = await session.step(GameAction("Y"))
                self.assertTrue(dead.ended)
                self.assertEqual(dead.outcome, "death")
                self.assertIsNone(session._process.returncode)
                self.assertTrue(scores[-1][2])
                self.assertIsInstance(scores[-1][0], int)
                self.assertIsInstance(scores[-1][3], int)
                with self.assertRaisesRegex(RuntimeError, "not accepting"):
                    await session.step(GameAction("ENTER"))
            finally:
                await session.close()

    async def test_ignored_input_has_new_boundary_and_clean_shutdown(self):
        messages = []
        scores = []
        with tempfile.TemporaryDirectory(prefix="dcss-test-") as directory:
            session = DCSSGameSession(os.environ["DCSS_TEST_BINARY"], save_dir=directory,
                                      on_tiles=lambda batch: messages.extend(batch),
                                      on_score=lambda *event: scores.append(event))
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
                self.assertTrue(scores)
                self.assertTrue(all(message.get("msg") != "harness_score" for message in messages))
                self.assertTrue(all(len(event) == 4 for event in scores))
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
        scores = []
        with tempfile.TemporaryDirectory(prefix="dcss-test-") as directory:
            session = DCSSGameSession(os.environ["DCSS_TEST_BINARY"], save_dir=directory,
                extra_args=("-name", "TransportTest", "-species", "Minotaur", "-background", "Fighter"),
                on_tiles=lambda batch: messages.extend(batch),
                on_score=lambda *event: scores.append(event))
            try:
                first = await session.start()
                self.assertIn("TransportTest the Minotaur Fighter", first.screen)
                self.assertIn("choice of weapons", first.screen)
                self.assertIsNone(scores[-1][3])
                playing = await session.step(GameAction("a"))
                self.assertEqual(scores[-1][3], 0)
                self.assertIn("Health:", playing.screen)
                self.assertIn("map", [message["msg"] for message in messages])
                inventory = await session.step(GameAction("i"))
                self.assertIn("rapier", inventory.screen)
                self.assertNotIn("Health:", inventory.screen)
                await session.step(GameAction("ESC"))
                waited = await session.step(GameAction("."))
                self.assertIn("Time: 1.0", waited.screen)
                self.assertEqual(scores[-1][3], 10)
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
