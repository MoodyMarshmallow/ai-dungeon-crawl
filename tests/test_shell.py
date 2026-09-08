import asyncio
import json
import os
from pathlib import Path
import shlex
import shutil
import sys
import tempfile
import unittest
from unittest.mock import patch

from ai_dungeon_crawl.contracts import GameObservation, ScreenStyle, StopExecution
from ai_dungeon_crawl.shell import ShellTerminal


class ShellValidationTests(unittest.IsolatedAsyncioTestCase):
    async def test_unsupported_platform_fails_closed(self):
        terminal = ShellTerminal()
        with patch('ai_dungeon_crawl.shell.sys.platform', 'linux'):
            with self.assertRaisesRegex(RuntimeError, 'no unsafe fallback'):
                await terminal.execute_shell('echo unsafe', GameObservation(0, ''), None)
        self.assertIsNone(terminal._process)

    async def test_failed_manual_setup_preserves_error_and_cleans_workspace(self):
        terminal = ShellTerminal(manual_path='/nonexistent/crawl-test-manual')
        with patch('ai_dungeon_crawl.shell.sys.platform', 'darwin'), patch(
                'ai_dungeon_crawl.shell.shutil.which', return_value='/usr/bin/sandbox-exec'):
            with self.assertRaises(FileNotFoundError):
                await terminal.execute_shell('echo unsafe', GameObservation(0, ''), None)
        self.assertIsNone(terminal._directory)


@unittest.skipUnless(sys.platform == 'darwin' and shutil.which('sandbox-exec'),
                     'Shell integration requires macOS sandbox-exec')
class ShellTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.terminal = ShellTerminal(timeout_seconds=3)
        self.observation = GameObservation(1, '  screen  \n next ', width=12, height=2,
                                           styles=(ScreenStyle(0, 2, 3, fg='red'),), cursor=(1, 2))
        self.keys = []

    async def asyncTearDown(self):
        await self.terminal.close()

    async def press(self, action):
        self.keys.append(action.key)
        return GameObservation(len(self.keys) + 1, 'after ' + action.key)

    async def run_code(self, code):
        return await self.terminal.execute_shell(code, self.observation, self.press)

    async def test_cli_preserves_metadata_and_actions(self):
        result = await self.run_code('crawl observe --json; crawl press l; crawl observe --json')
        first, second = map(json.loads, result.output.splitlines())
        self.assertEqual(first['screen'], self.observation.screen)
        self.assertNotIn('text_runs', first)
        self.assertEqual(first['cursor'], [1, 2])
        self.assertEqual(first['style_palette'][0]['fg'], 'red')
        self.assertEqual(second['id'], 2)
        self.assertEqual(self.keys, ['l'])
        self.assertEqual(result.observation.id, 2)

    async def test_plain_cli_screen_remains_unchanged(self):
        result = await self.run_code('crawl observe')
        screen, metadata = result.output.rsplit('\n\n', 1)
        self.assertEqual(screen, self.observation.screen)
        self.assertEqual(json.loads(metadata)['cursor'], [1, 2])
        self.assertNotIn('screen', json.loads(metadata))

    async def test_keypress_is_silent_and_still_updates_observation(self):
        result = await self.run_code('crawl press l; crawl press l')
        self.assertEqual(result.status, 'ok')
        self.assertEqual(result.output, '')
        self.assertEqual(self.keys, ['l', 'l'])
        self.assertEqual(result.observation.id, 3)

    async def test_keypress_rejects_json_option_without_sending_key(self):
        result = await self.run_code('crawl press l --json')
        self.assertNotEqual(result.status, 'ok')
        self.assertIn('usage:', result.output)
        self.assertEqual(self.keys, [])

    async def test_workspace_persists_but_shell_and_cwd_reset(self):
        first = await self.run_code('echo saved > note; export LOCAL_VAR=secret; mkdir nested; cd nested')
        self.assertEqual(first.status, 'ok')
        result = await self.run_code('cat note; printf "var=%s\\n" "$LOCAL_VAR"; pwd')
        self.assertEqual(result.output.splitlines(), ['saved', 'var=', str(self.terminal.workspace)])

    async def test_standard_tools_and_python(self):
        result = await self.run_code("printf 'one\\ntwo\\n' | grep two | sed 's/two/three/'; python -c 'print(42)'")
        self.assertEqual(result.output, 'three\n42\n')
        self.assertEqual(result.status, 'ok')
        if shutil.which('rg'):
            result = await self.run_code("printf 'match\\n' | rg match")
            self.assertEqual(result.output, 'match\n')

    async def test_synthetic_secrets_and_manual_are_protected(self):
        with tempfile.TemporaryDirectory() as outside:
            secret = Path(outside) / 'secret'
            secret.write_text('SYNTHETIC_SECRET_ONLY')
            manual = Path(outside) / 'manual.rst'
            manual.write_text('Manual fixture\n')
            await self.terminal.close()
            self.terminal = ShellTerminal(manual_path=manual)
            code = f'''cat crawl_manual.rst
cat {shlex.quote(str(secret))}
ln -s {shlex.quote(str(secret))} secret-link
cat secret-link
echo changed > {shlex.quote(str(secret))}
echo changed > "$CRAWL_MANUAL"
chmod u+w "$CRAWL_MANUAL"
echo "credential=${{SYNTHETIC_API_KEY-unset}}"
'''
            with patch.dict(os.environ, {'SYNTHETIC_API_KEY': 'SYNTHETIC_ENV_SECRET'}):
                result = await self.run_code(code)
            self.assertIn('Manual fixture', result.output)
            self.assertNotIn('SYNTHETIC_SECRET_ONLY', result.output)
            self.assertNotIn('SYNTHETIC_ENV_SECRET', result.output)
            self.assertIn('credential=unset', result.output)
            self.assertEqual(secret.read_text(), 'SYNTHETIC_SECRET_ONLY')
            self.assertEqual(self.terminal.manual_path.read_text(), 'Manual fixture\n')

    async def test_tcp_and_other_unix_sockets_denied(self):
        with tempfile.TemporaryDirectory() as outside:
            path = str(Path(outside) / 'other.sock')
            unix = await asyncio.start_unix_server(lambda r, w: w.close(), path)
            tcp = await asyncio.start_server(lambda r, w: w.close(), '127.0.0.1', 0)
            port = tcp.sockets[0].getsockname()[1]
            try:
                code = f'''python - <<'PY'
import socket
for family, address in [(socket.AF_INET, ('127.0.0.1', {port})), (socket.AF_UNIX, {path!r})]:
    try:
        with socket.socket(family) as s:
            s.settimeout(.2)
            s.connect(address)
        print('CONNECTED')
    except PermissionError:
        print('DENIED')
PY'''
                result = await self.run_code(code)
                self.assertEqual(result.output, 'DENIED\nDENIED\n')
            finally:
                unix.close()
                tcp.close()
                await unix.wait_closed()
                await tcp.wait_closed()

    async def test_sessions_cannot_escape_and_background_is_killed(self):
        result = await self.run_code('''python - <<'PY'
import os
for operation in [os.setsid, lambda: os.setpgid(0, 0),
                  lambda: os.posix_spawn('/bin/sleep', ['sleep', '1'], os.environ, setpgroup=0),
                  lambda: os.posix_spawn('/bin/sleep', ['sleep', '1'], os.environ, setsid=True)]:
    try:
        operation()
        print('ESCAPED')
    except PermissionError:
        print('DENIED')
PY
sleep 30 &
echo $!
''')
        lines = result.output.splitlines()
        self.assertEqual(lines[:4], ['DENIED'] * 4)
        child = int(lines[4])
        for _ in range(20):
            try:
                os.kill(child, 0)
            except ProcessLookupError:
                break
            await asyncio.sleep(.01)
        else:
            self.fail('Background child survived submission cleanup')

    async def test_timeout_and_output_are_bounded(self):
        await self.terminal.close()
        self.terminal = ShellTerminal(timeout_seconds=.4, max_output_chars=100)
        result = await self.run_code("python -c 'while True: print(\"x\" * 1000)' ")
        self.assertEqual(result.status, 'timeout')
        self.assertEqual(len(result.output), 100)
        self.assertTrue(result.output_truncated)
        self.assertIsNone(self.terminal._process)
        self.assertIsNone(self.terminal._directory)

    async def test_budget_and_failure_never_retry(self):
        count = 0
        async def limited(action):
            nonlocal count
            count += 1
            raise StopExecution('budget reached')
        result = await self.terminal.execute_shell(
            'for i in 1 2 3 4 5; do crawl press l & done; wait', self.observation, limited)
        self.assertEqual(result.status, 'stopped')
        self.assertEqual(count, 1)
        await self.terminal.close()
        self.terminal = ShellTerminal()
        async def failed(action):
            nonlocal count
            count += 1
            raise RuntimeError('sent but observation failed')
        with self.assertRaisesRegex(RuntimeError, 'sent but observation failed'):
            await self.terminal.execute_shell('crawl press l; crawl press l', self.observation, failed)
        self.assertEqual(count, 2)

    async def test_cancellation_during_action_closes_everything(self):
        started = asyncio.Event()
        cancelled = asyncio.Event()
        async def blocked(action):
            started.set()
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.set()
        task = asyncio.create_task(self.terminal.execute_shell('crawl press l', self.observation, blocked))
        await asyncio.wait_for(started.wait(), 3)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await asyncio.wait_for(task, 3)
        self.assertTrue(cancelled.is_set())
        self.assertIsNone(self.terminal._process)
        self.assertIsNone(self.terminal._directory)

    async def test_invalid_requests_and_concurrent_actions(self):
        result = await self.run_code('crawl press invalid; for i in 1 2 3; do crawl press l & done; wait')
        self.assertEqual(self.keys, ['l', 'l', 'l'])
        self.assertEqual(result.observation.id, 4)

    async def test_oversized_and_forged_broker_frames_cannot_act(self):
        result = await self.run_code('''python - <<'PY'
import json, os, socket
requests = [b'x' * 2048 + b'\\n', b'[]\\n',
            json.dumps({'type': 'press', 'key': ['l']}).encode() + b'\\n']
for request in requests:
    with socket.socket(socket.AF_UNIX) as connection:
        connection.connect(os.environ['CRAWL_SOCKET'])
        connection.sendall(request)
        with connection.makefile('rb') as reader:
            print('error' in json.loads(reader.readline()))
PY
crawl observe --json''')
        self.assertEqual(result.status, 'ok')
        self.assertEqual(result.output.splitlines()[:3], ['True'] * 3)
        self.assertEqual(self.keys, [])

    async def test_action_finishes_when_client_exits_early(self):
        async def slow(action):
            await asyncio.sleep(.2)
            return GameObservation(2, 'complete')
        result = await self.terminal.execute_shell('''python - <<'PY'
import socket, os, time
s = socket.socket(socket.AF_UNIX)
s.connect(os.environ['CRAWL_SOCKET'])
s.sendall(b'{"type":"press","key":"l"}\\n')
time.sleep(.05)
PY''', self.observation, slow)
        self.assertEqual(result.observation.screen, 'complete')
        self.assertEqual(result.status, 'ok')
