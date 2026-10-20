"""Caller service-registry declarations for the single local Runtime owner.

This layer owns configuration conversion, not process execution or agent policy.
All deployment names, repositories and paths come from the caller's node config.
"""
import json
import os
from pathlib import Path
import socket
import time
from .contract import fields,name,require,validate,no_duplicates
from .prepare import canonical,digest
from .kernel import publish
from experimental.file_owner.owner import Owner

PROFILE_KEYS=frozenset(('name','version','summary','note','notes','cmd','match','stop_match','cwd','wrapper','stop_cmd','status','start_timeout','env_file','env_loader','require_env','extra_env'))
DAEMON_KEYS=frozenset(('hostname','profile','desired','note'))

def document(path):
    result=json.loads(Path(path).read_text(),object_pairs_hook=no_duplicates)
    require(type(result) is dict,'configuration object required')
    return result


def node_config(path):
    c=document(path)
    fields(c,('apiVersion','node','root','protocol','workspace','python','home','path','sources'),
           ('services','external','bootstrapService','bootstrapSource','inheritEnv','interval',
            'registryFile','seedServices','bootstrapVersion'))
    require(c['apiVersion']=='rund-node/v1','unsupported node config')
    name(c['node'])
    for key in ('root','protocol','workspace','python','home'):
        require(type(c[key]) is str and Path(c[key]).is_absolute(),'absolute node '+key+' required')
    require(type(c['sources']) is dict and bool(c['sources']),'named source pins required')
    for alias,source in c['sources'].items():
        require(alias==source.get('alias'),'source key/alias mismatch')
    require(type(c.get('services',{})) is dict,'service bindings required')
    for service,b in c.get('services',{}).items():
        name(service);fields(b,(),('argv','sources','env','context'))
    require(type(c.get('external',[])) is list,'external names must be list')
    for service in c.get('external',[]):name(service)
    interval=c.get('interval',60)
    require(type(interval) in (int,float) and .1<=interval<=3600,'invalid registry interval')
    if 'registryFile' in c:
        require(type(c['registryFile']) is str and Path(c['registryFile']).is_absolute(), 'absolute registryFile required')
        require(not c.get('services') and not c.get('external'), 'shared registry cannot hide service bindings in node config')
        from .desired import check, envelope
        check(envelope(dict(node=c['node'], generation=1, services=c.get('seedServices',[]))), c['node'])
        require(type(c.get('bootstrapVersion')) is int and c['bootstrapVersion']>0, 'positive bootstrapVersion required')
    return c


def validate_profile(p):
    require(p.keys()<=PROFILE_KEYS,'unknown profile fields')
    require(type(p.get('version')) is int and p['version']>0,'positive profile version required')
    def argv(value):return type(value) is list and bool(value) and all(type(a) is str and a and '\x00' not in a for a in value)
    require(argv(p.get('cmd')),'profile argv required')
    require(p.get('wrapper','svc') in ('svc','none'),'invalid wrapper')
    for key in ('stop_cmd','env_loader'):
        if key in p:require(argv(p[key]),'invalid '+key)
    if 'env_loader' in p:require('env_file' in p,'env_loader requires env_file')
    for key in ('env_file','cwd'):
        if key in p:require(type(p[key]) is str and p[key] and '\x00' not in p[key],'invalid '+key)
    from .environment import KEY
    require(type(p.get('extra_env',{})) is dict,'extra_env must be object')
    for key,value in p.get('extra_env',{}).items():
        require(KEY.fullmatch(key) is not None and type(value) in (str,int,float,bool) and '\x00' not in str(value),'invalid extra_env')
    required=p.get('require_env',[])
    require(type(required) is list and all(type(k) is str and KEY.fullmatch(k) for k in required),'invalid require_env')
    if 'status' in p:
        probe=p['status'];require(type(probe) is dict,'invalid status probe')
        if 'http' in probe:
            require(probe.keys()<= {'http','timeout'} and type(probe['http']) is str and probe['http'].startswith(('http://','https://')),'invalid HTTP probe')
            require(type(probe.get('timeout',5)) in (int,float) and 0<probe.get('timeout',5)<30,'invalid HTTP timeout')
        else:
            require(probe.keys()<= {'cmd','expect'} and argv(probe.get('cmd')) and type(probe.get('expect','')) is str,'invalid command probe')
    return p


def services_here(c):
    if 'registryFile' in c:
        from .desired import selected
        result,error=selected(c)
        # Status shows the retained last-good desired state; snapshot diagnostics
        # carry any rejected/missing input. Bootstrap is outside its own graph.
        return [dict(name=c.get('bootstrapService','service-sync'), version=c['bootstrapVersion'],
                     desired='online', cmd=[c['python']])] + result
    workspace=Path(c['workspace']);profiles=workspace/'services/profiles';daemons=workspace/'services/daemons'
    here=socket.gethostname().lower();result=[];seen=set()
    for path in sorted(daemons.glob('*.json')):
        d=document(path);require(d.keys()<=DAEMON_KEYS,'unknown daemon fields')
        require(set(('hostname','profile','desired'))<=d.keys(),'incomplete daemon')
        host=d['hostname'].lower()
        if not (here==host or here.startswith(host+'.')):continue
        service=d['profile'];name(service)
        require(path.stem==c['node']+'.'+service,'daemon node/name mismatch')
        require(service not in seen,'duplicate local service');seen.add(service)
        require(d['desired'] in ('online','offline'),'invalid desired state')
        p=validate_profile(document(profiles/(service+'.json')))
        require('name' not in p or p['name']==service,'profile name mismatch')
        require(type(p.get('version')) is int and p['version']>0,'positive profile version required')
        require(type(p.get('cmd')) is list and p['cmd'] and all(type(a) is str and a for a in p['cmd']),'profile argv required')
        require(p.get('wrapper','svc') in ('svc','none'),'invalid wrapper')
        result.append(dict(p,name=service,desired=d['desired']))
    return result


def expand(value,c):
    require(type(value) is str,'string argument required')
    return c['home']+value[1:] if value=='~' or value.startswith('~/') else value


def absolute(value,c):
    p=Path(expand(value,c))
    return str((p if p.is_absolute() else Path(c['workspace'])/p).resolve())


def declaration(c,svc):
    if '_declaration' in svc:
        import copy
        d=copy.deepcopy(svc['_declaration'])
        # Online/offline is a control intent, not an executable version change.
        d['spec']['desired']='online'
        return validate(d)
    service=svc['name'];binding=c.get('services',{}).get(service,{})
    require(svc.get('wrapper','svc')=='svc' or 'argv' in binding,
            'self-daemonizing service requires an explicit foreground binding')
    aliases=binding.get('sources',list(c['sources']))
    require(type(aliases) is list and aliases,'explicit service source selection required')
    require(all(a in c['sources'] for a in aliases),'unknown service source alias')
    env=dict(PLATFORM_WORKSPACE=c['workspace'],PYTHONDONTWRITEBYTECODE='1')
    env.update({str(k):expand(str(v),c) for k,v in svc.get('extra_env',{}).items()})
    env.update(binding.get('env',{}))
    context=dict(cwd=absolute(svc.get('cwd') or '.',c),home=c['home'],path=c['path'],inheritEnv=c.get('inheritEnv',[]))
    context.update(binding.get('context',{}))
    refs=[]
    if svc.get('env_file') or svc.get('require_env'):
        loader=[expand(a,c) for a in svc.get('env_loader',[])]
        # The registry's loader argv is relative to the caller workspace.
        if loader:
            for i,a in enumerate(loader):
                if not a.startswith('-') and '/' in a and not Path(a).is_absolute():loader[i]=absolute(a,c)
        refs=[dict(file=absolute(svc['env_file'],c) if svc.get('env_file') else '/dev/null',loader=loader,require=svc.get('require_env',[]))]
    argv=[expand(a,c) for a in binding.get('argv',svc['cmd'])]
    d=dict(apiVersion='rund/v1alpha1',kind='Service',metadata=dict(id='svc-'+service,version=svc['version']),
           spec=dict(node=c['node'],sources=[c['sources'][a] for a in aliases],prepare=[],
                     toolchain={'python':c['python']},argv=argv,cwd='${work}',env=env,context=context,
                     credentialRefs=refs,desired='online',restart=dict(policy='always',initialSeconds=1,maxSeconds=60)))
    return validate(d)


class RegistryOwner(Owner):
    def __init__(self,config,path=None):
        self.config_path=Path(path).resolve() if path else None
        self.config=config;self.registry_errors=[];self.next_registry=0
        super().__init__(config['root'],config['protocol'],config['node'])

    def reconcile_registry(self):
        self.registry_errors=[]
        if self.config_path:
            latest=node_config(self.config_path)
            require(all(latest[k]==self.config[k] for k in ('node','root','protocol','workspace','python','home')),'live node identity/context root changed')
            self.config=latest
        blocked,errors=self.pending()
        if errors:
            self.registry_errors.extend(errors);return
        if 'registryFile' in self.config:
            from .desired import selected
            services,error=selected(self.config,accept=True)
            if error:self.registry_errors.append(error)
        else:services=services_here(self.config)
        for svc in services:
            key=svc['name']
            if key==self.config.get('bootstrapService','service-sync') or key in self.config.get('external',[]) or svc.get('external'):continue
            id='svc-'+key
            if ('Service',id) in blocked:continue
            try:
                head_path=self.runtime.root/'state/objects/Service'/id/'service.json'
                head=self.runtime.service(id) if head_path.exists() else None
                if svc['desired']=='offline':
                    if head and head['generation'] and not head['hold']:
                        self.runtime.control(self.control(id,head,'stop'))
                    continue
                d=declaration(self.config,svc)
                if head is None or head['declaration']!=d:
                    require(head is None or d['metadata']['version']>head['declaration']['metadata']['version'],
                            'changed service declaration requires a profile version bump')
                    request='registry-'+digest(canonical(d))[:40]
                    # Retry only a *completed, pre-spawn* preparation failure.
                    # Pending/unknown outcomes retain the original exact ID.
                    retry=0
                    while True:
                        candidate=request+('-r'+str(retry) if retry else '')
                        receipt=self.runtime.root/'state/requests'/(candidate+'.json')
                        if not receipt.exists():break
                        prior=document(receipt)
                        result=prior.get('result',{})
                        if (prior.get('status')!='done' or result.get('phase')!='failed' or
                            'previousUnchanged' not in result or not result.get('reason','').startswith('prepare: ')):break
                        retry+=1
                    value=self.runtime.submit_service(d,candidate)
                    if value.get('phase') in ('failed','cleanupUnknown'):
                        self.registry_errors.append(key+': '+value.get('reason','declaration failed'))
                elif head['hold'] and head['generation']:
                    self.runtime.control(self.control(id,head,'restart'))
            except (OSError,ValueError,KeyError,TypeError) as error:
                self.registry_errors.append(key+': '+str(error))

    def control(self,id,head,action):
        return dict(apiVersion='rund-control/v1alpha1',kind='Service',id=id,attemptId='service',
                    expectedGeneration=head['generation'],action=action,
                    requestId='registry-'+action+'-'+digest(canonical([id,head['generation'],head['declaration']]))[:40])

    def round(self,batch=16):
        if time.monotonic()>=self.next_registry:
            try:self.reconcile_registry()
            except (OSError,ValueError,KeyError,TypeError) as error:self.registry_errors=[str(error)]
            self.next_registry=time.monotonic()+self.config.get('interval',60)
        value=super().round(batch)
        value['diagnostics'].extend(self.registry_errors)
        if 'registryFile' in self.config:
            accepted=self.runtime.root/'registry-accepted.json'
            if accepted.exists():
                current=document(accepted)
                value['registry']=dict(generation=current['snapshot']['payload']['generation'],
                                       sha256=current['inputDigest'])
        publish(self.protocol/'snapshot.json',value)
        return value
