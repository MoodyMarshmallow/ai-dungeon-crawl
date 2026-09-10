from dataclasses import asdict
import json
from pathlib import Path
import tempfile
import unittest

from ai_dungeon_crawl.contracts import GameObservation, ScreenStyle
from ai_dungeon_crawl.events import emit
from ai_dungeon_crawl.game.observation_json import observation_data
from ai_dungeon_crawl.observation_history import read_observations
from ai_dungeon_crawl.run_log import episode_log


class ObservationHistoryTests(unittest.TestCase):
    def test_reads_flushed_screens_with_inclusive_ticks_and_repeated_times(self):
        with tempfile.TemporaryDirectory() as directory, episode_log(Path(directory)):
            first = GameObservation(0, '界 ', width=3, height=1,
                                    styles=(ScreenStyle(0, 0, 2, fg='red'),), cursor=(0, 0))
            emit('game.observation', **asdict(first))
            self.assertEqual(read_observations(), [
                {**observation_data(first), 'timestamp': None, 'sequence': 0}])
            for index, tick in enumerate((0, 10, 10, 25), start=1):
                emit('game.score', score=index, game_time=tick)
                emit('game.step', key='l', observation=asdict(GameObservation(index, str(index))))
            emit('model.message', text='not a screen')
            self.assertEqual(read_observations()[0]['id'], 4)
            self.assertEqual([r['id'] for r in read_observations(limit=2)], [3, 4])
            matches = read_observations(since=10, until=10)
            self.assertEqual([r['id'] for r in matches], [2, 3])
            self.assertNotEqual(matches[0]['sequence'], matches[1]['sequence'])
            self.assertEqual([r['timestamp'] for r in matches], [10, 10])
            self.assertEqual(read_observations(since=26), [])
            self.assertEqual([r['id'] for r in read_observations(until=0)], [1])
            self.assertNotIn('real_timestamp', json.dumps(read_observations(limit=100)))

    def test_limits_and_validation(self):
        with tempfile.TemporaryDirectory() as directory, episode_log(Path(directory)):
            emit('game.score', game_time=0)
            for index in range(105):
                emit('game.step', observation=asdict(GameObservation(index, 'x')))
            self.assertEqual(len(read_observations(since=0)), 20)
            self.assertEqual(len(read_observations(limit=100)), 100)
            for query in ({'since': -1}, {'until': True}, {'since': '2026-01-01'},
                          {'limit': 0}, {'limit': 101}, {'limit': 1.5},
                          {'since': 10, 'until': 0}):
                with self.subTest(query=query), self.assertRaises(ValueError):
                    read_observations(**query)

    def test_run_isolation_and_partial_final_line(self):
        with tempfile.TemporaryDirectory() as directory:
            with episode_log(Path(directory) / 'first') as path:
                emit('game.observation', **asdict(GameObservation(1, 'first')))
                with episode_log(Path(directory) / 'second'):
                    self.assertEqual(read_observations(), [])
                    emit('game.observation', **asdict(GameObservation(2, 'second')))
                    self.assertEqual(read_observations()[0]['id'], 2)
                with (path.parent / 'game.jsonl').open('a') as output:
                    output.write('{"unfinished":')
                self.assertEqual(read_observations()[0]['id'], 1)
        with self.assertRaisesRegex(ValueError, 'No observation journal'):
            read_observations()

    def test_observe_never_opens_model_or_tile_journals(self):
        with tempfile.TemporaryDirectory() as directory, episode_log(Path(directory)) as path:
            emit('game.observation', **asdict(GameObservation(1, 'game screen')))
            emit('model.message', text='private model trajectory')
            emit('game.tiles', messages=[])
            # Fail if observe even attempts to open either other trajectory.
            from unittest.mock import patch
            original_open = Path.open
            opened = []
            def game_only(file, *args, **kwargs):
                self.assertEqual(file, path.parent / 'game.jsonl')
                opened.append(file)
                return original_open(file, *args, **kwargs)
            with patch.object(Path, 'open', game_only):
                screens = read_observations(limit=100)
            self.assertEqual(len(opened), 1)
            self.assertEqual(screens[0]['rows'], ['game screen'])
            self.assertNotIn('private model trajectory', json.dumps(screens))
