import io
import json
import unittest
from contextlib import redirect_stdout, redirect_stderr
from unittest.mock import patch

from ai_dungeon_crawl._shell_client import main


class ShellClientTests(unittest.TestCase):
    def test_help_never_connects_to_game(self):
        for args in (['--help'], ['observe', '--help'], ['press', '--help']):
            with self.subTest(args=args), patch('sys.argv', ['crawl', *args]), \
                    patch('ai_dungeon_crawl._shell_client.socket.socket') as socket, \
                    redirect_stdout(io.StringIO()) as output:
                with self.assertRaises(SystemExit) as raised:
                    main()
                self.assertEqual(raised.exception.code, 0)
                self.assertIn('usage: crawl', output.getvalue())
                socket.assert_not_called()

    def test_invalid_arguments_never_connect_to_game(self):
        for args in ([], ['unknown'], ['press'], ['press', 'l', '--json'],
                     ['observe', 'extra'], ['observe', '--json']):
            with self.subTest(args=args), patch('sys.argv', ['crawl', *args]), \
                    patch('ai_dungeon_crawl._shell_client.socket.socket') as socket, \
                    redirect_stderr(io.StringIO()) as output:
                with self.assertRaises(SystemExit) as raised:
                    main()
                self.assertEqual(raised.exception.code, 2)
                self.assertIn('usage: crawl', output.getvalue())
                socket.assert_not_called()

    def test_commands_preserve_requests_and_output(self):
        observation = {'rows': [' 界 '], 'styles': [[0, 0, 0, 0]]}
        for args, request, response, expected in (
            (['observe'], {'type': 'observe'}, {'observation': observation},
             json.dumps(observation, ensure_ascii=False, separators=(',', ':')) + '\n'),
            (['press', 'l'], {'type': 'press', 'key': 'l'}, {}, ''),
            (['press', '-'], {'type': 'press', 'key': '-'}, {}, ''),
            (['press', 'CTRL+P'], {'type': 'press', 'key': 'CTRL+P'}, {}, ''),
        ):
            with self.subTest(args=args), patch('sys.argv', ['crawl', *args]), \
                    patch.dict('os.environ', {'CRAWL_SOCKET': '/test/socket'}), \
                    patch('ai_dungeon_crawl._shell_client.socket.socket') as socket, \
                    redirect_stdout(io.StringIO()) as output:
                connection = socket.return_value.__enter__.return_value
                reader = connection.makefile.return_value.__enter__.return_value
                reader.readline.return_value = (json.dumps(response) + '\n').encode()
                main()
                connection.sendall.assert_called_once_with((json.dumps(request) + '\n').encode())
                self.assertEqual(output.getvalue(), expected)
