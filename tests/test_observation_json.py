import json
import unittest
from dataclasses import asdict

from wcwidth import wcswidth

from ai_dungeon_crawl.contracts import GameObservation, ScreenStyle
from ai_dungeon_crawl.game.observation_json import observation_data, format_observation


class ObservationJsonTests(unittest.TestCase):
    def test_all_text_spaces_rows_and_style_attributes_round_trip(self):
        original = GameObservation(3, '  #.@ ~\n       \n       ', True, 7, 3,
            (ScreenStyle(0, 0, 7, 'red'),
             ScreenStyle(1, 0, 2, fg='white', bg='black', bold=True),
             ScreenStyle(1, 2, 1, bg='000000', italics=True),
             ScreenStyle(1, 3, 1, bg='brightblack', underline=True),
             ScreenStyle(1, 4, 1, reverse=True, blink=True)), (1, 6))
        before = asdict(original)
        result = json.loads(format_observation(observation_data(original)))
        self.assertEqual(set(result), {'id', 'ended', 'width', 'height', 'rows', 'palette', 'styles'})
        self.assertEqual(result['rows'], original.screen.split('\n'))
        self.assertEqual((result['id'], result['ended']), (3, True))
        self.assertEqual((result['width'], result['height']), (7, 3))
        defaults = dict(fg='default', bg='default', bold=False, italics=False,
                        underline=False, reverse=False, blink=False, cursor=False)
        for row in range(3):
            self.assertEqual(len(result['styles'][row]), 7)
            for col in range(7):
                expected = defaults.copy()
                for style in original.styles:
                    if style.row == row and style.col <= col < style.col + style.length:
                        expected.update({key: value for key, value in asdict(style).items()
                                         if key in defaults})
                expected['cursor'] = original.cursor == (row, col)
                self.assertEqual(result['palette'][result['styles'][row][col]], expected)
        self.assertEqual(asdict(original), before)

    def test_blank_screen_pads_missing_rows_and_deduplicates_complete_styles(self):
        result = observation_data(GameObservation(2, ' ' * 80, False, 80, 24))
        self.assertEqual(result['rows'], [' ' * 80] * 24)
        self.assertEqual(result['styles'], [[0] * 80] * 24)
        self.assertEqual(len(result['palette']), 1)
        self.assertEqual(len(result['palette'][0]), 8)
        self.assertFalse(result['palette'][0]['cursor'])

    def test_unicode_terminal_columns_keep_combining_marks_and_wide_cell_styles(self):
        result = observation_data(GameObservation(0, ' 界e\u0301 @', False, 7, 1,
            (ScreenStyle(0, 1, 2, bg='blue'), ScreenStyle(0, 3, 1, 'red')), (0, 2)))
        self.assertEqual(result['rows'], [' 界e\u0301 @ '])
        self.assertEqual(wcswidth(result['rows'][0]), 7)
        cells = [result['palette'][index] for index in result['styles'][0]]
        self.assertEqual([cell['bg'] for cell in cells],
                         ['default', 'blue', 'blue', 'default', 'default', 'default', 'default'])
        self.assertEqual(cells[3]['fg'], 'red')
        self.assertEqual([cell['cursor'] for cell in cells], [False, False, True, False, False, False, False])

    def test_missing_dimensions_are_inferred_in_terminal_columns(self):
        result = observation_data(GameObservation(0, '界@\n .'))
        self.assertEqual((result['width'], result['height']), (3, 2))
        self.assertEqual(result['rows'], ['界@', ' . '])
        self.assertEqual(result['styles'], [[0, 0, 0], [0, 0, 0]])

    def test_format_is_one_compact_json_line_with_every_blank_row(self):
        result = observation_data(GameObservation(0, '   \n @ \n   '))
        encoded = format_observation(result)
        self.assertEqual(len(encoded.splitlines()), 1)
        self.assertEqual(json.loads(encoded), result)
        self.assertNotIn(': ', encoded)


if __name__ == '__main__':
    unittest.main()
