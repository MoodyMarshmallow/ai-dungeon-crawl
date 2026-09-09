import json
import unittest
from ai_dungeon_crawl.contracts import (
    AgentTurn,
    AgentTurnRecord,
    ExecutionResult,
    GameObservation,
    GameStep,
    GameAction,
    ScreenStyle,
)
from ai_dungeon_crawl.agent.policies import _prompt
from ai_dungeon_crawl.game.observation_json import observation_data


class ScreenObservationTests(unittest.TestCase):
    def test_contract_keeps_legacy_positional_prefix_and_immutable_metadata(self):
        style = ScreenStyle(1, 2, 3, "red", bold=True)
        observation = GameObservation(7, "screen", True, 80, 24, (style,), (1, 2))

        self.assertEqual((observation.id, observation.screen, observation.ended),
                         (7, "screen", True))
        self.assertEqual(observation.width, 80)
        self.assertEqual(observation.height, 24)
        self.assertIsInstance(observation.styles, tuple)
        self.assertIsInstance(observation.cursor, tuple)
        with self.assertRaises(AttributeError):
            observation.width = 81

    def test_prompt_keeps_current_visual_metadata_but_compacts_history(self):
        current = GameObservation(1, "next", False, 80, 24,
                                  (ScreenStyle(0, 0, 1, "red"),), (0, 1))
        before = GameObservation(0, "before", False, 80, 24,
                                 (ScreenStyle(0, 0, 1, "red"),), (0, 1))
        record = AgentTurnRecord(
            0, AgentTurn("pass"), ExecutionResult(current, "feedback"),
            (GameStep(before, GameAction("l"), current),),
        )

        payload = json.loads(_prompt(current, (record,), "goal", 1, 10000))
        self.assertEqual(payload['observation'], observation_data(current))
        self.assertNotIn("observation", payload["history"][0]["execution"])
        self.assertEqual(payload["history"][0]["execution"]["output"], "feedback")

        later = GameObservation(2, "later")
        payload = json.loads(_prompt(later, (record,), "goal", 1, 10000))
        self.assertEqual(payload["history"][0]["execution"]["observation"], observation_data(current))

        budget = len(_prompt(later, (), 'goal', 1, 10000)) + 100
        payload = json.loads(_prompt(later, (record,), 'goal', 1, budget))
        self.assertEqual(payload['observation'], observation_data(later))
        self.assertEqual(payload['history'], [])
        self.assertEqual(payload['omitted_turns'], 1)

    def test_dense_style_runs_are_lossless_and_fit_default_context(self):
        styles = tuple(ScreenStyle(row=i // 100, col=i % 100, length=1,
                                   fg="red" if i % 2 else "blue",
                                   bold=bool(i % 3 == 0)) for i in range(3000))
        observation = GameObservation(1, "\n".join(["x" * 100] * 30), False, 100, 30, styles, (0, 0))

        prompt = _prompt(observation, (), "goal", 0, 64000)
        self.assertLess(len(prompt), 64000)
        payload = json.loads(prompt)
        self.assertLess(len(json.dumps(payload, ensure_ascii=False)), 64000)
        recovered = []
        for row, indexes in enumerate(payload['observation']['styles']):
            for col, palette_index in enumerate(indexes):
                attrs = dict(payload['observation']['palette'][palette_index])
                self.assertEqual(attrs.pop('cursor'), (row, col) == (0, 0))
                recovered.append(ScreenStyle(row, col, 1, **attrs))
        self.assertEqual(tuple(recovered), styles)


if __name__ == "__main__":
    unittest.main()
