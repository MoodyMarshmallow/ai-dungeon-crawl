import json
import os
import socket
import sys


def main():
    """Send one untrusted request; authority belongs to the parent broker."""
    args = sys.argv[1:]
    structured = args == ['observe', '--json']
    if args == ['observe'] or structured:
        request = {'type': 'observe'}
    elif len(args) == 2 and args[0] == 'press':
        request = {'type': 'press', 'key': args[1]}
    else:
        raise SystemExit('usage: crawl observe [--json] | crawl press KEY')
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
    if structured:
        print(json.dumps(response['observation']))
    elif request['type'] == 'observe':
        print(response['display'])


if __name__ == '__main__':
    main()
