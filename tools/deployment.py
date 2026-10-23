"""Bring up a node from a caller-owned configuration checkout."""
import copy
import hashlib
import json
import os
from pathlib import Path
import shlex
import shutil
import socket
import subprocess
import time
import uuid

from runtime.config import discover,node_config
from runtime.store import digest,read,require,write
from tools.prepare import build
from tools.source_plan import relative,validate


# Runs at the declaration origin, using that node's installed protocol library.
# Configuration attachments are immutable; declarations retain their revisions.
PUBLISH = '''import json,sys
from pathlib import Path
c=json.loads(Path(sys.argv[1]).read_text());sys.path.insert(0,c['runtimeSource'])
from runtime.config import publish
from runtime.store import digest,read,require,write
bundle=json.load(sys.stdin);root=Path(c['sharedRoot'])
for key,value in bundle['configs'].items():
 require(digest(value)==key,'configuration digest mismatch')
 p=root/'configs'/(key+'.json')
 if p.exists():require(read(p)==value,'immutable configuration conflict')
 else:write(p,value)
for d in bundle['declarations']:publish(root,d,writer=c['node'],publishers=c['publishers'])
'''


def inside(root,value):
    p=root/relative(value)
    require(p.resolve().is_relative_to(root.resolve()),'configuration reference escapes checkout')
    return p


def load(root,node=None):
    root=Path(root).resolve();plan=read(root/'bootstrap.json')
    require(plan.get('format')=='rund-bootstrap/v1','unsupported bootstrap format')
    hosts=plan.get('hosts',{})
    if isinstance(hosts,str):
        lines=inside(root,hosts).read_text().splitlines();hosts={}
        for line in lines:
            if not line.strip() or line.lstrip().startswith('#'):continue
            host,identifier=line.split();require(host not in hosts,'duplicate hostname')
            hosts[host]=identifier
    require(isinstance(hosts,dict),'hostname mapping must be an object or file')
    node=node or hosts.get(socket.gethostname())
    require(node in plan['nodes'],'node not configured; select --node or register hostname')
    entry=plan['nodes'][node];config=inside(root,entry['config']);c=node_config(config)
    require(c['node']==node,'node configuration identity mismatch')
    prefix=Path(c['privateRoot']).parent
    require(Path(c['privateRoot'])==prefix/'local','bootstrap privateRoot must be prefix/local')
    sync=read(inside(root,entry['sync']))
    require(c.get('syncConfig')==str(prefix/'local/sync.json'),'bootstrap syncConfig must be prefix/local/sync.json')
    require(sync.get('nodeConfig')==str(prefix/'local/node.json'),'transport must reference installed node configuration')
    sources=copy.deepcopy(plan['sources']);validate({'sources':sources})
    runtime=next((s for s in sources if s['alias']==plan['runtime']),None)
    require(runtime is not None,'runtime source alias missing')
    require(c.get('runtimeSource')==str(prefix/'runtime'/runtime['revision']),'runtime pin and node configuration differ')
    require(any(Path(sync['tool']).is_relative_to(prefix/'runtime'/s['revision']) for s in sources),'transport tool must use a bootstrap source pin')
    require(len({s['revision'] for s in sources})==len(sources),'duplicate bootstrap revision')
    found,errors=discover(inside(root,plan['objects']));require(not errors,'; '.join(errors))
    services={i:d for i,(_,d) in found.items() if d['node']==node and d['kind']=='Service'}
    require(c.get('bootstrapService') in services,'bootstrap Service declaration missing')
    require(services[c['bootstrapService']]['desired']=='online','bootstrap Service must be desired online')
    documents=[d for _,d in found.values() if d['node']==node]
    configs={}
    for p in (inside(root,plan['objects'])/'configs').glob('*.json'):
        value=read(p);require(digest(value)==p.stem,'configuration digest mismatch: '+p.name);configs[p.stem]=value
    for d in documents:require(d['publisher'] in c['publishers'],'unknown declaration publisher')
    for value in entry.get('requiredFiles',[]):require(Path(value).is_file(),'missing bootstrap prerequisite: '+value)
    require(Path(c['python']).is_file() and os.access(c['python'],os.X_OK),'configured Python unavailable')
    target=prefix/'local/node.json'
    if target.exists():
        require(read(target)==c,'installed node configuration differs; explicit upgrade required')
        require(read(prefix/'local/sync.json')==sync,'installed transport differs; explicit upgrade required')
    return root,plan,entry,c,sync,sources,services,dict(configs=configs,declarations=documents)


def export_sources(c,sources,runtime_alias):
    prefix=Path(c['privateRoot']).parent
    # The mechanism checkout is a source of fixed Git objects, never runtime cwd.
    source=Path(__file__).resolve().parents[1]
    for s in sources:
        if s['alias']==runtime_alias:
            has=subprocess.run(['git','-C',str(source),'cat-file','-e',s['revision']+'^{commit}'],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
            if has.returncode==0:s['repository']=source.as_uri()
    plan={'sources':sources};artifact=Path(c['privateRoot'])/'bootstrap-sources'/digest(plan)
    result=build(plan,artifact,Path(c['privateRoot'])/'source-cache')
    for s in sources:
        destination=prefix/'runtime'/s['revision'];export=artifact/s['alias']
        if destination.exists():
            inventory=result['files'][s['alias']]
            for rel,info in inventory.items():
                p=destination/rel
                require(not p.is_symlink(),'runtime source symlink refused')
                if info['gitMode']=='40000':require(p.is_dir(),'runtime directory missing')
                else:require(p.is_file() and hashlib.sha256(p.read_bytes()).hexdigest()==info['sha256'],'runtime source content differs: '+str(p))
        else:
            destination.parent.mkdir(mode=0o700,parents=True,exist_ok=True)
            stage=destination.with_name('.bootstrap-'+uuid.uuid4().hex)
            shutil.copytree(export,stage);stage.chmod(0o700)
            os.rename(stage,destination);destination.chmod(0o555)


def publish_inputs(root,plan,c,bundle):
    groups={}
    for d in bundle['declarations']:
        origin=c['publishers'][d['publisher']];groups.setdefault(origin,[]).append(d)
    for origin,documents in groups.items():
        origin_config=node_config(inside(root,plan['nodes'][origin]['config']))
        path=str(Path(origin_config['privateRoot'])/'node.json')
        argv=[origin_config['python'],'-B','-c',PUBLISH,path]
        if origin!=c['node']:argv=['ssh','-o','BatchMode=yes',origin,shlex.join(argv)]
        subprocess.run(argv,input=json.dumps(dict(configs=bundle['configs'],declarations=documents)),text=True,check=True,timeout=120)
        cli=str(Path(origin_config['privateRoot']).parent/'bin/runctl')
        sync=[cli,'sync']
        if origin!=c['node']:sync=['ssh','-o','BatchMode=yes',origin,shlex.join(sync)]
        subprocess.run(sync,check=True,stdout=subprocess.DEVNULL,timeout=130)


def bootstrap(root,node=None,*,check=False,timeout=300):
    root,plan,entry,c,sync,sources,services,bundle=load(root,node)
    if check:return dict(node=c['node'],services=len(services),runtime=c['runtimeSource'],checked=True)
    prefix=Path(c['privateRoot']).parent
    # Write transport only after all configuration validation has succeeded.
    # Existing installations above must match exactly; this is not an upgrade API.
    export_sync=prefix/'local/sync.json'
    if not export_sync.exists():write(export_sync,sync)
    for source in sources:source.setdefault('sshConfig',sync['sshConfig'])
    export_sources(c,sources,plan['runtime'])
    from tools.bootstrap import install
    if not (prefix/'local/node.json').exists():
        install(prefix,c,c['runtimeSource'],Path(c['runtimeSource']).name,upgrade=True)
    publish_inputs(root,plan,c,bundle)
    cli=str(prefix/'bin/runctl')
    subprocess.run([cli,'sync'],check=True,stdout=subprocess.DEVNULL,timeout=130)
    subprocess.run([cli,'start',c['bootstrapService']],check=True,stdout=subprocess.DEVNULL,timeout=160)
    end=time.monotonic()+timeout;health=None
    while time.monotonic()<end:
        result=subprocess.run([cli,'status'],capture_output=True,text=True,check=True,timeout=30);health=json.loads(result.stdout)
        rows={s['id']:s for s in health['services']}
        pending=[i for i,s in services.items() if i not in rows or rows[i]['desired']!=s['desired'] or rows[i]['version']!=s['version'] or rows[i]['state']!='ok']
        if not pending and not health['drift'] and not health['diagnostics'] and not health['blockedTargets']:
            receipt=dict(node=c['node'],runtime=c['runtimeSource'],deploymentDigest=digest(dict(plan=plan,node=c,sync=sync,inputs=bundle)),status=health)
            write(prefix/'local/bootstrap-result.json',receipt);return receipt
        time.sleep(.5)
    raise ValueError('bootstrap convergence pending: '+json.dumps(health))
