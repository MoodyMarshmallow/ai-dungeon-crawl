import json
import unittest
from dataclasses import asdict

from ai_dungeon_crawl.contracts import GameObservation, ScreenStyle
from ai_dungeon_crawl.observation_json import observation_data as sparse_observation, format_observation


class SparseObservationTests(unittest.TestCase):
    def test_readable_output_omits_blank_lines_without_changing_coordinates(self):
        original = GameObservation(0, '   \n  @  \n\n \t \n . \n  ',
                                   styles=(ScreenStyle(4, 1, 1, fg='red'),), cursor=(4, 1))
        data = sparse_observation(original)
        screen, metadata = format_observation(data).rsplit('\n\n', 1)
        metadata = json.loads(metadata)
        self.assertEqual(screen, '  @  \n . ')
        self.assertEqual(metadata['screen_rows'], [1, 4])
        self.assertEqual(metadata['style_runs'], [[4, 1, 1, 0]])
        self.assertEqual(metadata['cursor'], [4, 1])
        self.assertEqual(data['screen'], original.screen)
        self.assertNotIn('screen_rows', data)

    def test_readable_blank_screen_has_no_text_rows(self):
        screen, metadata = format_observation(sparse_observation(
            GameObservation(0, '   \n\n  '))).rsplit('\n\n', 1)
        self.assertEqual(screen, '')
        self.assertEqual(json.loads(metadata)['screen_rows'], [])

    def test_sentences_and_columns_stay_in_one_row(self):
        line = "  Welcome, Bot.    Health: 19/19  "
        result = sparse_observation(GameObservation(0, line, width=len(line), height=1,
                    styles=(ScreenStyle(0, 0, len(line), fg="white", bg="black"),)))
        self.assertEqual(result["screen"], line)
        screen, metadata = format_observation(result).rsplit("\n\n", 1)
        self.assertEqual(screen, line)
        self.assertNotIn("screen", json.loads(metadata))
        self.assertNotIn("text_runs", result)

    def test_blank_screen_has_no_cell_payload(self):
        result = sparse_observation(GameObservation(2, " " * 80, False, 80, 24))
        self.assertEqual(result["screen"], " " * 80)
        self.assertEqual(result["style_runs"], [])
        self.assertEqual(result["style_palette"], [])
        self.assertEqual((result["width"], result["height"]), (80, 24))
        self.assertNotIn("text_runs", result)

    def test_terrain_and_positions_are_not_filtered_by_meaning(self):
        result = sparse_observation(GameObservation(3, "  #.@ ~\n       ", True, 7, 2,
            (ScreenStyle(0, 0, 7, "red"),), (0, 4)))
        self.assertEqual(result["screen"], "  #.@ ~\n       ")
        self.assertEqual(result["style_runs"], [[0, 2, 3, 0], [0, 6, 1, 0]])
        self.assertEqual(result["cursor"], (0, 4))
        self.assertTrue(result["ended"])

    def test_visible_spaces_preserved_foreground_only_spaces_omitted(self):
        styles = (ScreenStyle(0, 0, 1, "red"), ScreenStyle(0, 1, 1, bg="blue"),
                  ScreenStyle(0, 2, 1, underline=True), ScreenStyle(0, 3, 1, reverse=True))
        result = sparse_observation(GameObservation(0, "    ", False, 4, 1, styles))
        self.assertEqual(result["screen"], "    ")
        self.assertEqual(len(result["style_runs"]), 3)
        self.assertNotIn({"fg": "red"}, result["style_palette"])

    def test_explicit_black_background_is_empty_but_gray_is_visible(self):
        styles = (ScreenStyle(0, 0, 2, fg="white", bg="black"),
                  ScreenStyle(0, 2, 1, bg="000000"),
                  ScreenStyle(0, 3, 1, bg="brightblack"))
        result = sparse_observation(GameObservation(0, "    ", False, 4, 1, styles))
        self.assertEqual(result["screen"], "    ")
        self.assertEqual(result["style_palette"], [{"bg": "brightblack"}])

    def test_unicode_uses_terminal_columns_and_retains_combining_marks(self):
        result = sparse_observation(GameObservation(0, " 界e\u0301 @", False, 7, 1,
                                    (ScreenStyle(0, 3, 1, "red"),)))
        self.assertEqual(result["screen"], " 界e\u0301 @")
        self.assertEqual(result["style_runs"], [[0, 3, 1, 0]])

    def test_sparse_background_is_small_and_original_is_unchanged(self):
        text = "\n".join([" " * 100] * 15 + [" " * 50 + "@" + " " * 49] + [" " * 100] * 14)
        original = GameObservation(1, text, False, 100, 30,
                                   tuple(ScreenStyle(row, 0, 100, "white") for row in range(30)))
        result = sparse_observation(original)
        metadata = {k: v for k, v in result.items() if k != "screen"}
        self.assertLess(len(json.dumps(metadata)), len(json.dumps(asdict(original))) / 10)
        self.assertEqual(result["screen"], text)
        self.assertEqual(original.screen, text)

    def test_missing_dimensions_are_inferred_for_mock_observations(self):
        result = sparse_observation(GameObservation(0, "界@\n ."))
        self.assertEqual((result["width"], result["height"]), (3, 2))


if __name__ == "__main__":
    unittest.main()
