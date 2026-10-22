"""Test-only source export CLI; no runtime execution entry."""
import argparse
import json
import sys
from tools.source_contract import load
from tools.source_export import prepare

def main(argv=None):
    p=argparse.ArgumentParser();s=p.add_subparsers(dest='action',required=True)
    for a in ('validate','prepare'):
        c=s.add_parser(a);c.add_argument('--declaration',required=True)
        if a=='prepare':c.add_argument('--root',required=True)
    a=p.parse_args(argv)
    try:
        d=load(a.declaration);value=prepare(d,a.root) if a.action=='prepare' else dict(valid=True,kind=d['kind'],id=d['metadata']['id'])
        print(json.dumps(value));return 0
    except (ValueError,OSError,UnicodeError,__import__('subprocess').SubprocessError) as e:print('source export error: '+str(e),file=sys.stderr);return 2

if __name__=='__main__':sys.exit(main())
