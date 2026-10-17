"""Opt-in experiment: validate/prepare never execute; execute is explicit only."""
import argparse
import json
import sys
from .contract import load
from .prepare import prepare


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='command', required=True)
    for command in ('validate', 'prepare'):
        p = sub.add_parser(command)
        p.add_argument('--declaration', required=True)
        if command == 'prepare':
            p.add_argument('--root', required=True)
    p = sub.add_parser('execute', help='explicit finite lifecycle operation batch, no daemon')
    p.add_argument('--root', required=True)
    p.add_argument('--node', required=True)
    p.add_argument('--operations', required=True, help='absolute JSON file containing operation list')
    args = parser.parse_args(argv)
    try:
        if args.command == 'execute':
            from .operations import execute
            result = execute(args.root, args.node, args.operations)
        else:
            d = load(args.declaration)
            result = {'valid': True, 'kind': d['kind'], 'id': d['metadata']['id']} if args.command == 'validate' else prepare(d, args.root)
        print(json.dumps(result, sort_keys=True))
        return 0
    except (ValueError, OSError, UnicodeError, __import__('subprocess').SubprocessError) as e:
        print('rund prepare error: ' + str(e), file=sys.stderr)
        return 2


if __name__ == '__main__':
    sys.exit(main())
