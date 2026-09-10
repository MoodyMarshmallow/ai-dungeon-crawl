import argparse
from pathlib import Path
import unittest
from unittest.mock import patch

from ai_dungeon_crawl.cli import DEFAULT_CRAWL_PATH, add_game_arguments, create_game
from ai_dungeon_crawl.game.mock_game import MockGameSession


class GameSelectionTests(unittest.TestCase):
    def test_parser_defaults_to_dcss_and_sibling_crawl_path(self):
        parser = argparse.ArgumentParser()
        add_game_arguments(parser)
        args = parser.parse_args([])
        self.assertEqual(args.game, "dcss")
        self.assertEqual(args.crawl_path, DEFAULT_CRAWL_PATH)
        self.assertEqual(DEFAULT_CRAWL_PATH, Path(__file__).resolve().parents[2] /
                         "crawl/crawl-ref/source/crawl-web-harness")

    def test_mock_game_is_explicit_and_does_not_need_binary(self):
        game = create_game("mock", Path("/definitely/not/a/dcss/binary"))
        self.assertIsInstance(game, MockGameSession)

    def test_missing_dcss_binary_fails_closed(self):
        with self.assertRaisesRegex(FileNotFoundError, "DCSS build missing"):
            create_game("dcss", Path("/definitely/not/a/dcss/binary"))

    def test_dcss_wires_score_events_without_changing_tile_events(self):
        with patch("ai_dungeon_crawl.game.dcss.DCSSGameSession") as session_type, \
                patch("ai_dungeon_crawl.cli.emit") as emit:
            executable = Path(__file__).resolve()
            game = create_game("dcss", executable)
            self.assertIs(game, session_type.return_value)
            kwargs = session_type.call_args.kwargs
            kwargs["on_tiles"]([{"msg": "map"}])
            kwargs["on_score"](42, 9, True, 90)
            emit.assert_any_call("game.tiles", messages=[{"msg": "map"}])
            emit.assert_any_call("game.score", score=42, game_turn=9, final=True, game_time=90)


if __name__ == "__main__":
    unittest.main()
