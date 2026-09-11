import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from pydantic_ai.messages import ModelResponse, ToolCallPart, UserPromptPart
from pydantic_ai.models.function import FunctionModel

from ai_dungeon_crawl.agent.prompts import ActionPrompts, MAX_PROMPT_BYTES, base_prompts, load_prompts, read_only_prompt
from ai_dungeon_crawl.agent.policies import PydanticPolicy, ShellScript
from ai_dungeon_crawl.contracts import AgentTurn, AgentTurnRecord, ExecutionResult, GameObservation
from ai_dungeon_crawl.session import run_session
from ai_dungeon_crawl.shell.review_files import prepare_files, publish_artifacts
from test_session import FakeGame, FakeTerminal


class PromptFilesTests(unittest.TestCase):
    def test_front_matter_roundtrip_and_body_only_effective_text(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            work, store = root / 'work', root / 'store'
            directory = work / 'prompts/agent_editable/action'
            base = base_prompts()
            base.write(directory)
            originals = {p.name: p.read_text() for p in directory.iterdir()}
            self.assertTrue(all(text.startswith('---\npurpose:') for text in originals.values()))
            document = directory / 'initial_prompt.md'
            revised = document.read_text().replace('purpose: Starts a new action episode with its objective.',
                                                   'purpose: "Reviewer-only metadata revision."')
            document.write_text(revised)
            publish_artifacts(work, store)
            self.assertEqual((store / 'snapshot/prompts/agent_editable/action/initial_prompt.md').read_text(), revised)
            effective = load_prompts(store / 'snapshot/prompts/agent_editable/action')
            self.assertEqual(effective.effective_text(), base.effective_text())
            effective.write(root / 'next-copy')
            self.assertEqual((root / 'next-copy/initial_prompt.md').read_text(), revised)

    def test_malformed_front_matter_never_publishes_helpers_or_prompts(self):
        headers = [
            '---\npurpose: note\nusage: note\n',  # missing closing delimiter
            '---\npurpose: note\n---\nbody',  # missing field
            '---\npurpose: note\npurpose: duplicate\nusage: note\n---\nbody',
            '---\npurpose: note\nusage: note\nrole: system\n---\nbody',
            '---\npurpose: "unterminated\nusage: note\n---\nbody',
            '---\npurpose: 123\nusage: note\n---\nbody',
            '---\npurpose: nested: value\nusage: note\n---\nbody',
            '---\npurpose: !unsafe text\nusage: note\n---\nbody',
            '---\npurpose: note\nusage: |\n  nested\n---\nbody',
            '---\npurpose: note\nusage: note\n---\n ',
            ' ---\npurpose: note\nusage: note\n---\nbody',
            '\n---\npurpose: note\nusage: note\n---\nbody',
            '---\npurpose: ' + 'x' * 4096 + '\nusage: note\n---\nbody',
        ]
        for text in headers:
            with self.subTest(header=text[:80]), tempfile.TemporaryDirectory() as temp:
                root = Path(temp)
                work, store = root / 'work', root / 'store'
                directory = work / 'prompts/agent_editable/action'
                base_prompts().write(directory)
                (work / 'tools').mkdir()
                (work / 'tools/helper').write_text('old')
                publish_artifacts(work, store)
                (work / 'tools/helper').write_text('new')
                (directory / 'system_prompt.md').write_text(text)
                with self.assertRaises(ValueError):
                    publish_artifacts(work, store)
                self.assertEqual((store / 'snapshot/tools/helper').read_text(), 'old')
                self.assertEqual(load_prompts(store / 'snapshot/prompts/agent_editable/action').effective_text(),
                                 base_prompts().effective_text())

    def test_invalid_prompt_and_helper_never_partially_publish(self):
        for invalid in ('empty', 'nul', 'utf8', 'large', 'multibyte', 'symlink', 'fifo', 'directory', 'missing', 'extra', 'root-link'):
            with self.subTest(invalid=invalid), tempfile.TemporaryDirectory() as temp:
                root = Path(temp)
                work, store = root / 'work', root / 'store'
                base_prompts().write(work / 'prompts/agent_editable/action')
                (work / 'tools').mkdir()
                (work / 'tools/helper').write_text('old')
                publish_artifacts(work, store)
                (work / 'tools/helper').write_text('new')
                prompt = work / 'prompts/agent_editable/action/initial_prompt.md'
                if invalid in ('empty', 'nul', 'utf8', 'large', 'multibyte'):
                    prompt.write_bytes({'empty': b' ', 'nul': b'x\0y', 'utf8': b'\xff',
                                        'large': b'x' * (MAX_PROMPT_BYTES + 1),
                                        'multibyte': ('é' * MAX_PROMPT_BYTES).encode()}[invalid])
                elif invalid == 'extra':
                    (prompt.parent / 'capabilities.json').write_text('{}')
                elif invalid == 'root-link':
                    (work / 'prompts').rename(work / 'original-prompts')
                    (work / 'prompts').symlink_to(work / 'original-prompts', target_is_directory=True)
                else:
                    prompt.unlink()
                    if invalid == 'symlink':
                        prompt.symlink_to(root / 'absent')
                    elif invalid == 'fifo':
                        os.mkfifo(prompt)
                    elif invalid == 'directory':
                        prompt.mkdir()
                with self.assertRaises((ValueError, OSError)):
                    publish_artifacts(work, store)
                self.assertEqual((store / 'snapshot/tools/helper').read_text(), 'old')
                self.assertEqual(load_prompts(store / 'snapshot/prompts/agent_editable/action'), base_prompts())

    def test_published_copy_and_base_do_not_change_with_workspace_edits(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            work, store = root / 'work', root / 'store'
            base = base_prompts()
            base.write(work / 'prompts/agent_editable/action')
            (work / 'prompts/agent_editable/action/initial_prompt.md').write_text('Revised initial_prompt')
            publish_artifacts(work, store)
            snapshot = load_prompts(store / 'snapshot/prompts/agent_editable/action')
            (work / 'prompts/agent_editable/action/initial_prompt.md').write_text('Later edit')
            self.assertEqual(snapshot.initial_prompt, 'Revised initial_prompt')
            self.assertEqual(base_prompts(), base)


class PromptPolicyTests(unittest.IsolatedAsyncioTestCase):
    async def test_front_matter_is_absent_from_model_requests_and_effective_reference(self):
        with tempfile.TemporaryDirectory() as temp:
            directory = Path(temp) / 'prompts/agent_editable/action'
            base_prompts().write(directory)
            for document in directory.iterdir():
                body = document.read_text().split('---\n', 2)[2]
                document.write_text('---\npurpose: META_ONLY_PURPOSE\nusage: "META_ONLY_USAGE"\n---\n' + body)
            prompts = load_prompts(directory)
            requests = []
            def model(messages, info):
                requests.append(messages)
                initial = [p.content for m in messages for p in m.parts if isinstance(p, UserPromptPart)]
                self.assertEqual(initial, [prompts.initial_prompt])
                self.assertEqual(info.instructions, prompts.system_prompt.strip())
                reference = policy.prompt_reference()
                self.assertEqual(set(reference['prompts']), {'system_prompt', 'initial_prompt', 'execute_shell'})
                combined = str((initial, info.instructions, info.output_tools, reference))
                self.assertNotIn('META_ONLY', combined)
                self.assertNotIn('purpose:', combined)
                return ModelResponse(parts=[ToolCallPart('execute_shell', {'code': ':'}, f'call-{len(requests)}')])
            policy = PydanticPolicy(FunctionModel(model), prompts=prompts)
            observation = GameObservation(0, '')
            turn = await policy.request_turn(observation, ())
            await policy.request_turn(observation, (AgentTurnRecord(0, turn, ExecutionResult(observation, 'feedback'), ()),))
            self.assertEqual(len(requests), 2)

    async def test_read_only_markdown_drives_review_prompt_and_fixed_schema(self):
        initial_prompt = read_only_prompt('review_agent/initial_prompt', single_line=True)
        def model(messages, info):
            self.assertEqual(info.instructions, read_only_prompt('review_agent/system_prompt').strip())
            tool = info.output_tools[0]
            self.assertEqual(tool.description, read_only_prompt('review_agent/execute_shell') + '. ' +
                             ShellScript.__doc__)
            for field in ('code', 'timeout_ms'):
                self.assertEqual(tool.parameters_json_schema['properties'][field]['description'],
                                 ShellScript.model_fields[field].description)
            self.assertEqual([p.content for m in messages for p in m.parts if isinstance(p, UserPromptPart)], [initial_prompt])
            return ModelResponse(parts=[ToolCallPart('execute_shell', {'code': ':'})])
        await PydanticPolicy(FunctionModel(model), initial_prompt=initial_prompt, profile='review').request_turn(GameObservation(0, ''), ())

    async def test_effective_reference_and_initial_prompt_once_with_custom_prompts(self):
        prompts = ActionPrompts('Custom instructions', 'Custom initial_prompt', 'Custom shell description')
        requests = []
        def model(messages, info):
            requests.append(messages)
            reference = policy.prompt_reference()
            self.assertEqual(info.instructions, prompts.system_prompt)
            self.assertEqual(info.output_tools[0].description, reference['tool']['description'])
            self.assertEqual(info.output_tools[0].parameters_json_schema, reference['tool']['parameters_json_schema'])
            initial_prompts = [p.content for m in messages for p in m.parts if isinstance(p, UserPromptPart)]
            self.assertEqual(initial_prompts, [prompts.initial_prompt])
            return ModelResponse(parts=[ToolCallPart('execute_shell', {'code': ':'}, f'call-{len(requests)}')])
        policy = PydanticPolicy(FunctionModel(model), prompts=prompts)
        observation = GameObservation(0, 'not automatically sent')
        turn = await policy.request_turn(observation, ())
        await policy.request_turn(observation, (AgentTurnRecord(0, turn, ExecutionResult(observation, 'feedback'), ()),))
        self.assertEqual(len(requests), 2)

    async def test_review_edits_next_episode_and_new_start_resets(self):
        class PublishingTerminal(FakeTerminal):
            async def publish_artifacts(self):
                with tempfile.TemporaryDirectory() as temp:
                    root = Path(temp)
                    work = root / 'work'
                    work.mkdir()
                    prepare_files(root, work, profile='review', artifacts_path=self.kwargs['artifacts_path'],
                                  review_log_path=self.kwargs['review_log_path'])
                    (work / 'prompts/agent_editable/action/initial_prompt.md').write_text('Reviewed initial_prompt')
                    publish_artifacts(work, self.kwargs['artifacts_path'])
                await super().publish_artifacts()

        async def start(directory, review_limit):
            seen = []
            outcomes = iter(['death', 'win'])
            def factory(profile, *, prompts=None):
                if profile == 'action':
                    seen.append(prompts)
                class Policy:
                    async def request_turn(self, observation, history):
                        return AgentTurn(':')
                return Policy()
            await run_session(directory=directory, game_factory=lambda path: FakeGame(next(outcomes)),
                              policy_factory=factory, action_turn_limit=1, review_turn_limit=review_limit,
                              episode_limit=2)
            return seen

        with tempfile.TemporaryDirectory() as temp, patch('ai_dungeon_crawl.session.ShellTerminal', PublishingTerminal):
            root = Path(temp)
            seen = await start(root / 'first', 1)
            self.assertEqual([p.initial_prompt for p in seen], [base_prompts().initial_prompt, 'Reviewed initial_prompt'])
            self.assertEqual(seen[0], base_prompts())
            snapshot = json.loads((root / 'first/episode-002/action/action-prompts.json').read_text())
            self.assertEqual(snapshot['prompts']['initial_prompt'], 'Reviewed initial_prompt')
            self.assertEqual((root / 'first/action-prompts.json').stat().st_mode & 0o777, 0o600)
            fresh = await start(root / 'second', 0)
            self.assertEqual(fresh, [base_prompts(), base_prompts()])
            with self.assertRaises(FileExistsError):
                await start(root / 'first', 1)
            self.assertEqual(load_prompts(root / 'first/artifacts/snapshot/prompts/agent_editable/action').initial_prompt, 'Reviewed initial_prompt')
