import argparse
import json
import os
import socket


def main():
    """Send one untrusted request; authority belongs to the parent broker."""
    parser = argparse.ArgumentParser(prog='crawl', description='Observe and interact with the game.')
    commands = parser.add_subparsers(dest='command', required=True)
    observe = commands.add_parser('observe', help='Read recorded screens as JSON lines; latest by default.')
    observe.add_argument('--since', type=int, metavar='TICK', help='Inclusive minimum DCSS game tick (not real time).')
    observe.add_argument('--until', type=int, metavar='TICK', help='Inclusive maximum DCSS game tick (not real time).')
    observe.add_argument('-n', '--lines', dest='limit', type=int, metavar='N',
                         help='Latest N matching screens, oldest first (1–100; default 1, or 20 with time filters).')
    press = commands.add_parser('press', help='Send one key without printing an observation.')
    press.add_argument('key', metavar='KEY', help='One character or a named key, such as ENTER or CTRL+P.')
    args = parser.parse_args()
    if args.command == 'observe':
        request = {'type': 'observe'}
        request.update({key: getattr(args, key) for key in ('since', 'until', 'limit')
                        if getattr(args, key) is not None})
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
        for observation in response['observations']:
            print(json.dumps(observation, ensure_ascii=False, separators=(',', ':')))


if __name__ == '__main__':
    main()
