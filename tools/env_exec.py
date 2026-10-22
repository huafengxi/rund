#!/usr/bin/env python3
"""Task payload wrapper: load caller-declared credentials in memory, then exec."""
import argparse
import os
from pathlib import Path
import subprocess
import sys
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from runtime.store import read,require
from tools.source_environment import parse_env


def main():
    p=argparse.ArgumentParser();p.add_argument('--config',required=True,type=Path);p.add_argument('argv',nargs=argparse.REMAINDER);a=p.parse_args()
    c=read(a.config);env=dict(os.environ)
    for ref in c.get('files',[]):
        loader=ref.get('loader',[])
        if loader:
            r=subprocess.run(loader+[ref['file']],capture_output=True,timeout=30)
            require(r.returncode==0,'credential loader failed')
            content=r.stdout.decode('utf-8')
        else:content=Path(ref['file']).read_text()
        env.update(parse_env(content))
    require(all(env.get(k) for k in c.get('require',[])),'required credential missing')
    argv=a.argv[1:] if a.argv and a.argv[0]=='--' else a.argv
    require(bool(argv),'application argv required');os.execvpe(argv[0],argv,env)

if __name__=='__main__':
    try:main()
    except (OSError,ValueError,UnicodeError,subprocess.SubprocessError):print('credential injection unavailable',file=sys.stderr);sys.exit(126)
