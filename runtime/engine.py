"""Node execution: checkout-backed Services and independent one-shot Tasks."""
import copy
import os
from pathlib import Path
import signal
import time
import uuid
from .config import FINAL,changed,publish,declaration_revision
from .process import Processes,expand
from .store import digest,directory,lock,now,read,require,write
from .admission import admitted
from . import services

class Engine:
    def __init__(self,c):
        self.c=c;self.root=directory(c['sharedRoot']);self.private=directory(c['privateRoot']);self.stopped=False;self.paused=False
        self.fd=lock(self.private/'locks/runtime.lock');self.processes=Processes(c)
        self.accepted_path=self.private/'accepted.json';self.accepted=read(self.accepted_path) if self.accepted_path.exists() else {}
        self.objects={};self.errors=[]
        for k in ('artifacts','tasks','services'):directory(self.private/k)
        from .native import identity
        self.identity=identity(os.getpid());write(self.private/'runtime.json',dict(self.identity,node=c['node'],startedAt=now(),diagnostics=[],status='running',runtimeRevision=c.get('runtimeRevision')))
    def close(self):os.close(self.fd)
    def state(self,path):
        p=path.with_name('state.json');return read(p) if p.exists() else None
    def save(self,path,s,**values):
        prior=copy.deepcopy(s);s.update(values)
        if s==prior and path.with_name('state.json').exists():return s
        if s['kind']=='Task' and prior.get('phase') in FINAL:
            require(s==prior,'terminal Task state is immutable');return s
        s['updatedAt']=now();write(path.with_name('state.json'),s)
        if s['kind']=='Service' and s.get('runId'):
            write(self.private/'services'/s['id']/'runs'/s['runId']/'state.json',s)
        return s
    def initial(self,d):
        base=dict(kind=d['kind'],id=d['id'],node=d['node'],declarationDigest=digest(d),updatedAt=None,reason=None,
                  process=None,startedAt=None,finishedAt=None,exitCode=None,signal=None,waitObserved=False,runtimeRevision=self.c.get('runtimeRevision'))
        if d['kind']=='Service':return dict(base,phase='stopped',runId=None,runVersion=None,nextStartAt=None,restartCount=0)
        return dict(base,phase='accepted')
    def accept(self):
        found=services.objects(self.c);self.errors=[]
        direct,errors=services.declarations(self.c);self.errors.extend(errors)
        candidates={tid:(p,d) for tid,(p,d) in found.items() if d['kind']=='Task' and not d.get('service')}
        candidates.update({tid:(services.state_path(self.c,tid).with_name(source.name),d) for tid,(source,d) in direct.items()})
        for tid,(p,d) in candidates.items():
            if d['node']!=self.c['node']:continue
            try:
                if 'nodes' in self.c:require(d['node'] in self.c['nodes'],'unknown execution node')
                if d['kind']=='Task' and not p.exists():
                    publish(self.root,d,revision=declaration_revision(p))
                if tid in self.accepted:
                    previous=self.accepted[tid]
                    require(declaration_revision(p)>=declaration_revision(previous['path']),'declaration revision rollback')
                    if declaration_revision(p)==declaration_revision(previous['path']):require(previous['declaration']==d,'declaration version content conflict')
                    changed(previous['declaration'],d)
                self.accepted[tid]=dict(path=str(p.relative_to(self.root)),declaration=d)
            except (OSError,ValueError,KeyError,TypeError) as e:self.errors.append(tid+': '+str(e))
        write(self.accepted_path,self.accepted)
        self.objects={tid:(self.root/v['path'],v['declaration']) for tid,v in self.accepted.items()
                      if v['declaration']['node']==self.c['node'] and
                      (v['declaration']['kind']=='Task' and not v['declaration'].get('service') or tid in direct)}
        # Removing a declaration stops its accepted process before forgetting it.
        self.removed=set()
        for tid,v in self.accepted.items():
            d=v['declaration']
            if d['kind']=='Service' and 'task' not in d and tid not in direct:
                self.removed.add(tid)
                self.objects[tid]=(self.root/v['path'],dict(d,desired='offline'))
    def task(self,p,d):
        s=self.state(p) or self.initial(d)
        if s['phase'] in FINAL:return
        require(s['node']==self.c['node'] and s['id']==d['id'],'foreign execution state')
        s['declarationDigest']=digest(d)
        if s['phase'] in ('accepted','preparing','queued'):
            if d['desired']=='cancel':self.save(p,s,phase='canceled',finishedAt=now(),reason='canceled before spawn');return
            if self.paused:return
            missing=[f for f in d.get('needs',[]) if not Path(expand(f,self.c,d['id'])).is_file()]
            if missing:self.save(p,s,phase='preparing',reason='missing files: '+', '.join(missing));return
            if 'resources' in d:
                if not admitted(self.c,d):
                    self.save(p,s,phase='queued',queuedAt=s.get('queuedAt') or now(),reason='awaiting global admission');return
            self.save(p,s,phase='starting',reason='spawn intent')
            try:
                process=self.processes.spawn(d)
                self.save(p,s,phase='running',process=process,startedAt=now(),reason=None)
            except (OSError,ValueError,__import__('subprocess').SubprocessError) as e:
                if d['id'] in self.processes.handles:self.save(p,s,phase='unknown',reason='spawn identity unavailable')
                else:self.save(p,s,phase='failed',finishedAt=now(),reason='spawn failed: '+str(e))
            return
        fact=self.processes.observe(d['id'],s['process'])
        if fact['status']=='exited':
            rc=fact['rc'];phase='canceled' if s.get('cancelRequested') else ('succeeded' if rc==0 else 'failed')
            self.save(p,s,phase=phase,finishedAt=now(),exitCode=rc if rc>=0 else None,signal=-rc if rc<0 else None,waitObserved=True,reason=None);return
        if fact['status']=='unknown':
            self.save(p,s,phase='unknown',reason=fact.get('reason'));return
        if d['desired']=='cancel':
            step=s.get('stopStep',0);kill=d.get('cancelMode')=='kill' or step==1 and time.time()>=s.get('stopDeadline',0)
            if step==0 or step==1 and kill:
                try:
                    self.processes.signal(s['process'],kill)
                    self.save(p,s,phase='canceling',cancelRequested=True,stopStep=2 if kill else 1,stopDeadline=time.time()+3,reason='KILL sent' if kill else 'TERM sent')
                except (OSError,ValueError,RuntimeError) as e:self.save(p,s,phase='unknown',reason=str(e))
        elif s['phase']!='running' or time.time()-__import__('datetime').datetime.fromisoformat(s['updatedAt']).timestamp()>5:
            self.save(p,s,phase='running',reason=None,observedAt=now())
    def service(self,p,d):
        s=self.state(p) or self.initial(d)
        require('currentTask' not in s,'legacy Service state must be archived before migration')
        wanted='offline' if self.stopped or d['id'] in getattr(self,'removed',set()) else services.wanted(self.c,d)
        incompatible=s.get('runVersion') is not None and s['runVersion']!=d['version']
        active=s['phase'] in ('starting','running','stopping','unknown','blocked') and s.get('runId')
        if active:
            fact=self.processes.observe(d['id'],s['process'])
            if fact['status']=='unknown':
                self.save(p,s,phase='unknown',reason=fact.get('reason'));return
            if fact['status']=='running':
                if wanted=='offline' or incompatible:
                    step=s.get('stopStep',0);kill=step==1 and time.time()>=s.get('stopDeadline',0)
                    if step==0 or kill:
                        self.processes.signal(s['process'],kill)
                        self.save(p,s,phase='stopping',stopStep=2 if kill else 1,stopDeadline=time.time()+3,reason='KILL sent' if kill else 'TERM sent')
                elif s['phase']!='running':self.save(p,s,phase='running',reason=None)
                return
            rc=fact['rc']
            self.save(p,s,phase='stopped',finishedAt=now(),exitCode=rc if rc>=0 else None,signal=-rc if rc<0 else None,waitObserved=True,
                      reason='execution replaced' if incompatible else 'stopped by request' if wanted=='offline' or s.get('stopStep') else 'process exited')
        if wanted=='offline':
            self.save(p,s,declarationDigest=digest(d),phase='stopped',nextStartAt=None,reason='stopped by request');return
        first=s.get('runId') is None
        requested=s.get('reason') in ('stopped by request','execution replaced')
        if not first and not incompatible and not requested and s['phase'] not in ('preparing','queued'):
            retry=d['restart']=='always' or d['restart']=='on-failure' and (s.get('exitCode')!=0 or s.get('signal'))
            if not retry:self.save(p,s,phase='stopped',reason='restart policy holds');return
            if s['nextStartAt'] is None:
                n=s.get('restartCount',0)+1;self.save(p,s,phase='backoff',restartCount=n,nextStartAt=time.time()+min(30,.5*2**min(n-1,6)),reason='restart policy');return
            if time.time()<s['nextStartAt']:return
        if self.paused:return
        missing=[f for f in d.get('needs',[]) if not Path(expand(f,self.c,d['id'])).is_file()]
        if missing:self.save(p,s,phase='preparing',reason='missing files: '+', '.join(missing));return
        if 'resources' in d and not admitted(self.c,d):
            self.save(p,s,phase='queued',queuedAt=s.get('queuedAt') or now(),reason='awaiting global admission');return
        run=uuid.uuid4().hex
        self.save(p,s,phase='starting',runId=run,runVersion=d['version'],declarationDigest=digest(d),process=None,
                  nextStartAt=None,startedAt=None,finishedAt=None,exitCode=None,signal=None,waitObserved=False,
                  stopStep=0,reason='spawn intent',runtimeRevision=self.c.get('runtimeRevision'))
        try:
            process=self.processes.spawn(d,run_id=run)
            self.save(p,s,phase='running',process=process,startedAt=now(),reason=None)
        except (OSError,ValueError,__import__('subprocess').SubprocessError) as e:
            uncertain=d['id'] in self.processes.handles and self.processes.handles[d['id']].poll() is None
            self.save(p,s,phase='unknown' if uncertain else 'stopped',finishedAt=None if uncertain else now(),reason='spawn failed: '+str(e))
    def round(self):
        try:
            self.accept();self.paused=self.stopped
        except (OSError,ValueError,KeyError,TypeError) as e:
            self.errors=[str(e)];self.paused=True
        for p,d in list(self.objects.values()):
            try:
                if d['kind']=='Service':self.service(p,d)
                else:self.task(p,d)
            except (OSError,ValueError,KeyError,TypeError,RuntimeError) as e:self.errors.append(d['id']+': '+str(e))
        record=read(self.private/'runtime.json');record.update(observedAt=now(),diagnostics=self.errors,status='stopping' if self.stopped else 'running');write(self.private/'runtime.json',record)
    def closed(self):
        for p,d in self.objects.values():
            s=self.state(p)
            if s and (d['kind']=='Service' and s['phase']!='stopped' or d['kind']=='Task' and s['phase'] not in FINAL):return False
        return True
    def run(self):
        def stop(*unused):self.stopped=True;self.paused=True
        for sig in (signal.SIGTERM,signal.SIGINT):signal.signal(sig,stop)
        while True:
            self.round()
            if self.stopped and self.closed():
                record=read(self.private/'runtime.json');record.update(status='stopped',finishedAt=now());write(self.private/'runtime.json',record);return
            time.sleep(self.c.get('interval',.2))
