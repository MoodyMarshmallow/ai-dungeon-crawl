import asyncio
import json
import os
from pathlib import Path
import shlex
import shutil
import sys
import tempfile
import unittest

from ai_dungeon_crawl.contracts import GameObservation
from ai_dungeon_crawl.shell.terminal import ShellTerminal
from ai_dungeon_crawl.shell.review_files import publish_artifacts
from ai_dungeon_crawl.agent.prompts import base_prompts, read_only_prompt


class ArtifactTests(unittest.TestCase):
    def test_invalid_handoff_preserves_previous_artifacts(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            work, store = root / 'work', root / 'store'
            base_prompts().write(work / 'prompts' / 'agent_editable' / 'action')
            (work / 'tools').mkdir(parents=True)
            (work / 'tools' / 'helper.py').write_text('print(42)')
            publish_artifacts(work, store)
            (work / 'tools' / 'escape').symlink_to('/etc/passwd')
            with self.assertRaisesRegex(ValueError, 'symlinks'):
                publish_artifacts(work, store)
            self.assertEqual((store / 'snapshot/tools/helper.py').read_text(), 'print(42)')
            (work / 'tools' / 'escape').unlink()
            (work / 'tools' / 'huge').write_bytes(b'x' * (1024 * 1024 + 1))
            with self.assertRaisesRegex(ValueError, 'size limit'):
                publish_artifacts(work, store)
            self.assertEqual((store / 'snapshot/tools/helper.py').read_text(), 'print(42)')

    def test_special_files_and_directory_symlinks_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            work = root / 'work'
            work.mkdir()
            base_prompts().write(work / 'prompts' / 'agent_editable' / 'action')
            (work / 'tools').symlink_to(root, target_is_directory=True)
            with self.assertRaisesRegex(ValueError, 'real directories'):
                publish_artifacts(work, root / 'store')
            (work / 'tools').unlink()
            (work / 'tools').mkdir()
            os.mkfifo(work / 'tools' / 'pipe')
            with self.assertRaisesRegex(ValueError, 'regular file'):
                publish_artifacts(work, root / 'store')


@unittest.skipUnless(sys.platform == 'darwin' and shutil.which('sandbox-exec'),
                     'Shell integration requires macOS sandbox-exec')
class ReviewShellTests(unittest.IsolatedAsyncioTestCase):
    async def test_first_action_has_empty_read_only_artifact_directories(self):
        action = ShellTerminal()
        try:
            result = await action.execute_shell(
                'test -d tools && test -d skills && echo READY; ls -A tools skills',
                GameObservation(0, ''), None)
            self.assertEqual(result.status, 'ok')
            self.assertIn('READY', result.output)
            self.assertEqual(list((action.workspace / 'tools').iterdir()), [])
            self.assertEqual(list((action.workspace / 'skills').iterdir()), [])
            for name in ('tools', 'skills'):
                result = await action.execute_shell(f'echo forbidden > {name}/new',
                                                    GameObservation(0, ''), None)
                self.assertNotEqual(result.status, 'ok')
                self.assertFalse((action.workspace / name / 'new').exists())
        finally:
            await action.close()

    async def test_forged_game_requests_cannot_reach_existing_broker(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for name in ('model.jsonl', 'game.jsonl'):
                (root / name).write_text('{}\n')
            reached = []
            async def serve(reader, writer):
                reached.append(await reader.readline())
                writer.close()
            socket_path = str(root / 'game.sock')
            server = await asyncio.start_unix_server(serve, path=socket_path)
            review = ShellTerminal(profile='review', review_log_path=root)
            try:
                result = await review.execute_shell(f'''python - <<'PY'
import socket, json
for request in [{{'type': 'press', 'key': 'l'}}, {{'type': 'observe'}}]:
    with socket.socket(socket.AF_UNIX) as connection:
        try:
            connection.connect({socket_path!r})
            connection.sendall(json.dumps(request).encode() + b'\\n')
            print('CONNECTED')
        except PermissionError:
            print('DENIED')
PY''', GameObservation(0, ''), None)
                self.assertEqual(result.output, 'DENIED\nDENIED\n')
                self.assertEqual(reached, [])
            finally:
                await review.close()
                server.close()
                await server.wait_closed()

    async def test_review_boundary_and_action_handoff(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            logs = root / 'episode'
            logs.mkdir()
            for name in ('model.jsonl', 'game.jsonl'):
                (logs / name).write_text(json.dumps({'allowed': name}) + '\n')
            forbidden = logs / 'tiles.jsonl'
            (logs / 'action-prompts.json').write_text('{"prompts": "effective snapshot"}')
            forbidden.write_text('SECRET_TILES')
            store = root / 'store'
            review = ShellTerminal(profile='review', review_log_path=logs, artifacts_path=store)
            observation = GameObservation(0, '')
            async def forbidden_press(action):
                self.fail('Review invoked game control')
            try:
                result = await review.execute_shell('''cat logs/model.jsonl logs/game.jsonl
test -z "$CRAWL_SOCKET" && echo NO_SOCKET
command -v crawl || echo NO_CRAWL
echo 'print("helper output")' > tools/helper.py
echo strategy > skills/strategy.md
cat prompts/read_only/action-prompts.json
echo 'Reviewed initial_prompt' > prompts/agent_editable/action/initial_prompt.md
''', observation, forbidden_press)
                self.assertEqual(result.status, 'ok')
                self.assertIn('NO_SOCKET\nNO_CRAWL', result.output)
                self.assertIsNone(review._server)
                self.assertIn('effective snapshot', result.output)
                result = await review.execute_shell('''python - <<'PY'
from pathlib import Path
editable = Path('prompts/agent_editable/action/system_prompt.md').read_text()
assert editable.startswith('---\\npurpose:')
assert '\\nusage:' in editable
for name in ('review/system_prompt.md', 'review/initial_prompt.md', 'review/execute_shell.md'):
    path = Path('prompts/read_only') / name
    assert path.read_text()
    try:
        path.write_text('overwrite')
    except PermissionError:
        pass
    else:
        raise AssertionError('read-only prompt was writable')
print('READ_ONLY_PROMPTS_VERIFIED')
PY''', observation, forbidden_press)
                self.assertEqual(result.status, 'ok')
                self.assertIn('READ_ONLY_PROMPTS_VERIFIED', result.output)
                self.assertEqual((review.workspace / 'prompts/read_only/review/system_prompt.md').read_text(),
                                 read_only_prompt('review_agent/system_prompt'))
                originals = base_prompts()
                result = await review.execute_shell('''echo forbidden > prompts/read_only/action_originals/initial_prompt.md
chmod u+w prompts/read_only/action_originals/initial_prompt.md
echo forbidden > prompts/read_only/action-prompts.json
''', observation, forbidden_press)
                self.assertNotEqual(result.status, 'ok')
                original_document = (review.workspace / 'prompts/read_only/action_originals/initial_prompt.md').read_text()
                self.assertTrue(original_document.startswith('---\npurpose:'))
                self.assertTrue(original_document.endswith(originals.initial_prompt))
                self.assertIn('effective snapshot', (review.workspace / 'prompts/read_only/action-prompts.json').read_text())
                code = f'''cat {shlex.quote(str(forbidden))}
cat {shlex.quote(str(logs / 'model.jsonl'))}
echo changed > logs/model.jsonl
chmod u+w logs/model.jsonl
python - <<'PY'
import socket
s = socket.socket(socket.AF_UNIX)
try:
    s.connect({review._socket!r})
    s.sendall(b'{{"type":"press","key":"l"}}\\n')
    print('BAD_CONNECTION')
except (PermissionError, FileNotFoundError):
    print('DENIED')
PY'''
                result = await review.execute_shell(code, observation, forbidden_press)
                self.assertNotIn('SECRET_TILES', result.output)
                self.assertNotIn('allowed', result.output)
                self.assertNotIn('BAD_CONNECTION', result.output)
                self.assertIn('DENIED', result.output)
                self.assertIn('allowed', (logs / 'model.jsonl').read_text())
                self.assertIn('allowed', (review.workspace / 'logs/model.jsonl').read_text())
                await review.publish_artifacts()
                self.assertFalse((store / 'snapshot/prompts/read_only').exists())
            finally:
                await review.close()
            action = ShellTerminal(artifacts_path=store)
            try:
                result = await action.execute_shell('python tools/helper.py; cat skills/strategy.md',
                                                    observation, forbidden_press)
                self.assertEqual(result.output, 'helper output\nstrategy\n')
                result = await action.execute_shell('cat prompts/agent_editable/action/initial_prompt.md; echo forbidden > prompts/agent_editable/action/initial_prompt.md',
                                                    observation, forbidden_press)
                self.assertNotEqual(result.status, 'ok')
                self.assertTrue(result.output.startswith('Reviewed initial_prompt\n'))
                self.assertEqual((action.workspace / 'prompts/agent_editable/action/initial_prompt.md').read_text(), 'Reviewed initial_prompt\n')
                result = await action.execute_shell('echo changed > tools/helper.py', observation, forbidden_press)
                self.assertNotEqual(result.status, 'ok')
                self.assertIn('helper output', (store / 'snapshot/tools/helper.py').read_text())
                # A later publication cannot mutate this already provisioned action copy.
                (store / 'snapshot/tools/helper.py').write_text('print("new")')
                result = await action.execute_shell('python tools/helper.py', observation, forbidden_press)
                self.assertEqual(result.output, 'helper output\n')
            finally:
                await action.close()
