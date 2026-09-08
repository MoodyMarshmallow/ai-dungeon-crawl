import argparse
from pathlib import Path
import unittest

from ai_dungeon_crawl.cli import DEFAULT_CRAWL_PATH, add_game_arguments, create_game
from ai_dungeon_crawl.mock_game import MockGameSession


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


if __name__ == "__main__":
    unittest.main()
