"""Explicit foreground entry. Query/send never import the Runtime owner."""
import argparse
import json
import sys
from pathlib import Path

from .protocol import decode, read, receipt, send, snapshot


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    subs = ap.add_subparsers(dest='command', required=True)
    owner = subs.add_parser('owner')
    owner.add_argument('--root', required=True)
    owner.add_argument('--node', required=True)
    owner.add_argument('--protocol', required=True)
    sender = subs.add_parser('send')
    sender.add_argument('--protocol', required=True)
    sender.add_argument('--request', required=True)
    reader = subs.add_parser('receipt')
    reader.add_argument('--protocol', required=True)
    reader.add_argument('--rid', required=True)
    query = subs.add_parser('snapshot')
    query.add_argument('--protocol', required=True)
    args = ap.parse_args()
    try:
        if args.command == 'owner':
            from .owner import Owner
            instance = Owner(args.root, args.protocol, args.node)
            try:
                instance.run()
            finally:
                instance.close()
        elif args.command == 'send':
            print(json.dumps(send(args.protocol, decode(read(Path(args.request))))))
        elif args.command == 'receipt':
            print(json.dumps(receipt(args.protocol, args.rid)))
        else:
            print(json.dumps(snapshot(args.protocol)))
        return 0
    except (ValueError, OSError, KeyError, TypeError) as error:
        print('file-owner error: ' + str(error), file=sys.stderr)
        return 2


if __name__ == '__main__':
    sys.exit(main())
