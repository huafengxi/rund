#!/usr/bin/env python3
"""Caller-installed make launcher. Publisher routing is supplied by node config."""
import json
import os
from pathlib import Path
import shlex
import subprocess
import sys
import time
prefix=Path(__file__).resolve().parent
installation=json.loads((prefix/'runtime.json').read_text())
sys.path.insert(0,installation['runtime'])
from runtime.cli import status,launcher,owner_alive
from runtime.config import node_config
from runtime.protocol import objects,desired
from runtime.store import require


def main():
    config=prefix/'local/node.json';c=node_config(config);args=sys.argv[1:]
    if args==['status']:print(json.dumps(status(c)));return
    if args and args[0] in ('ctl','report'):
        entry=prefix/'workspace/agentd'/('agentctl.py' if args[0]=='ctl' else 'report.py')
        require(entry.is_file(),'agentd assets are not ready');env=dict(os.environ,PLATFORM_WORKSPACE=str(prefix/'workspace'))
        os.execve(c['python'],[c['python'],'-B',str(entry),'--root',str(prefix/'workspace'),*args[1:]],env)
    require(args and args[0]=='service','expected service command');args=args[1:]
    if args==['sync']:print(json.dumps(status(c)));return
    require(len(args)==2 and args[0] in ('start','stop','status'),'invalid service command')
    action,name=args
    if action=='status':
        row=next((s for s in status(c)['services'] if s['name']==name),None);require(row is not None,'service not declared');print(json.dumps(row));return
    if name=='service-sync':
        if action=='start' and c.get('syncConfig'):
            subprocess.run([c['python'],'-B',str(Path(installation['runtime'])/'tools/sync.py'),'--config',c['syncConfig'],'once'],timeout=100,check=False)
        launcher(c,config,action);return
    found=[(id,d) for id,(p,d) in objects(c['sharedRoot']).items() if d['kind']=='Service' and d['node']==c['node'] and d.get('name',id)==name]
    require(len(found)==1,'service selector not unique');id,d=found[0];value='online' if action=='start' else 'offline'
    origin=c['publishers'][d['publisher']]
    if origin==c['node']:desired(c['sharedRoot'],id,value,writer=c['node'],publishers=c['publishers'])
    else:
        ctrl=c['controller'];argv=[ctrl['python'],'-B',ctrl['runtime']+'/rund.py','--config',ctrl['config'],'desired',id,value]
        subprocess.run(['ssh','-o','BatchMode=yes',ctrl['host'],shlex.join(argv)],check=True,timeout=30,stdout=subprocess.DEVNULL)
    if name=='declaration-sync' and action=='start':
        subprocess.run([c['python'],'-B',installation['runtime']+'/tools/sync.py','--config',c['syncConfig'],'once'],timeout=100,check=True)
    end=time.monotonic()+120
    while time.monotonic()<end:
        row=next((s for s in status(c)['services'] if s['name']==name),None)
        if row and row['desired']==value and row['actual']==value and row['state']=='ok':print(json.dumps(row));return
        time.sleep(.3)
    raise ValueError('published desired state; actual remains pending')

if __name__=='__main__':
    try:main()
    except (OSError,ValueError,KeyError,subprocess.SubprocessError) as e:print('service launcher: '+str(e),file=sys.stderr);sys.exit(2)
