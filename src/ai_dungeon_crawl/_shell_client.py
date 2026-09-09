import argparse
import json
import os
import socket


def main():
    """Send one untrusted request; authority belongs to the parent broker."""
    parser = argparse.ArgumentParser(prog='crawl', description='Observe and interact with the game.')
    commands = parser.add_subparsers(dest='command', required=True)
    commands.add_parser('observe', help='Print the full game observation as JSON.')
    press = commands.add_parser('press', help='Send one key without printing an observation.')
    press.add_argument('key', metavar='KEY', help='One character or a named key, such as ENTER or CTRL+P.')
    args = parser.parse_args()
    if args.command == 'observe':
        request = {'type': 'observe'}
    else:
        request = {'type': 'press', 'key': args.key}
    try:
        with socket.socket(socket.AF_UNIX) as connection:
            connection.connect(os.environ['CRAWL_SOCKET'])
            connection.sendall((json.dumps(request) + '\n').encode())
            with connection.makefile('rb') as reader:
                line = reader.readline()
                if not line:
                    raise SystemExit('Shell submission stopped')
                response = json.loads(line)
    except (ConnectionError, FileNotFoundError):
        raise SystemExit('Shell submission stopped') from None
    if 'error' in response:
        raise SystemExit(response['error'])
    if request['type'] == 'observe':
        print(json.dumps(response['observation'], ensure_ascii=False, separators=(',', ':')))


if __name__ == '__main__':
    main()
