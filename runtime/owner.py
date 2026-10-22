"""One node Owner: file dependencies, Task execution and Service supervision."""
import copy
import os
from pathlib import Path
import signal
import time
import uuid
from .config import FINAL,changed,discover,publish,validate
from .process import Processes,expand
from .store import digest,directory,lock,now,read,require,write
from .scheduling import admitted,reconcile

class Owner:
    def __init__(self,c):
        self.c=c;self.root=directory(c['sharedRoot']);self.private=directory(c['privateRoot']);self.stopped=False
        self.fd=lock(self.private/'locks/owner.lock');self.processes=Processes(c)
        self.accepted_path=self.private/'accepted.json';self.accepted=read(self.accepted_path) if self.accepted_path.exists() else {}
        self.objects={};self.errors=[]
        for k in ('artifacts','tasks'):directory(self.private/k)
        from .native import identity
        self.identity=identity(os.getpid());write(self.private/'owner.json',dict(self.identity,startedAt=now(),diagnostics=[]))
    def close(self):os.close(self.fd)
    def state(self,path):
        p=path.with_name('state.json');return read(p) if p.exists() else None
    def save(self,path,s,**values):
        prior=copy.deepcopy(s);s.update(values)
        if s==prior and path.with_name('state.json').exists():return s
        if s['kind']=='Task' and prior.get('phase') in FINAL:
            require(s==prior,'terminal Task state is immutable');return s
        s['updatedAt']=now();write(path.with_name('state.json'),s);return s
    def initial(self,d):
        base=dict(kind=d['kind'],id=d['id'],node=d['node'],declarationDigest=digest(d),updatedAt=None,reason=None)
        if d['kind']=='Service':return dict(base,phase='stopped',currentTask=None,nextStartAt=None,restartCount=0)
        return dict(base,phase='accepted',process=None,startedAt=None,finishedAt=None,exitCode=None,signal=None,waitObserved=False)
    def accept(self):
        found,errors=discover(self.root);self.errors=errors
        for tid,(p,d) in found.items():
            if d['node']!=self.c['node']:continue
            try:
                require(d['publisher'] in self.c['publishers'],'unknown publisher')
                if tid in self.accepted:changed(self.accepted[tid]['declaration'],d)
                self.accepted[tid]=dict(path=str(p.relative_to(self.root)),declaration=d)
            except (OSError,ValueError,KeyError,TypeError) as e:self.errors.append(tid+': '+str(e))
        write(self.accepted_path,self.accepted)
        self.objects={tid:(self.root/v['path'],v['declaration']) for tid,v in self.accepted.items()}
    def cancel(self,tid):
        p,d=self.objects[tid]
        require(self.c['publishers'][d['publisher']]==self.c['node'],'cancellation must be written by declaration publisher')
        if d['desired']=='cancel':return
        d=dict(d,desired='cancel',revision=d['revision']+1)
        publish(self.root,d,writer=self.c['node'],publishers=self.c['publishers']);self.objects[tid]=(p,d)
    def task(self,p,d):
        s=self.state(p) or self.initial(d)
        if s['phase'] in FINAL:return
        require(s['node']==self.c['node'] and s['id']==d['id'],'foreign execution state')
        s['declarationDigest']=digest(d)
        if d.get('service'):
            parent=self.objects.get(d['service']['id']);ps=self.state(parent[0]) if parent else None
            if not ps or ps['currentTask']!=d['id']:return
        if s['phase'] in ('accepted','preparing','queued'):
            if d['desired']=='cancel':self.save(p,s,phase='canceled',finishedAt=now(),reason='canceled before spawn');return
            missing=[f for f in d.get('needs',[]) if not Path(expand(f,self.c,d['id'])).is_file()]
            if missing:self.save(p,s,phase='preparing',reason='missing files: '+', '.join(missing));return
            if 'resources' in d:
                require('scheduler' in self.c,'resources require a scheduler')
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
        s=self.state(p) or self.initial(d);current=self.objects.get(s['currentTask']);ts=self.state(current[0]) if current else None
        incompatible=current and current[1].get('service',{}).get('version')!=d['version']
        if current and (d['desired']=='offline' or incompatible):
            self.cancel(current[1]['id'])
            if ts is None or ts['phase'] not in FINAL:
                self.save(p,s,declarationDigest=digest(d),phase='blocked' if ts and ts['phase']=='unknown' else 'stopping',reason='awaiting previous Task closure');return
        if ts and ts['phase']=='unknown':self.save(p,s,phase='blocked',reason='current Task outcome unknown');return
        if d['desired']=='offline':self.save(p,s,declarationDigest=digest(d),phase='stopped',nextStartAt=None,reason=None);return
        if ts and ts['phase'] not in FINAL:
            self.save(p,s,declarationDigest=digest(d),phase='running' if ts['phase']=='running' else 'starting',reason=ts.get('reason'));return
        should_start=not current or incompatible or (s['phase']=='stopped' and s.get('reason')!='restart policy holds')
        if current and ts and ts['phase'] in FINAL and not should_start:
            retry=d['restart']=='always' or d['restart']=='on-failure' and ts['phase']=='failed'
            if not retry:self.save(p,s,phase='stopped',reason='restart policy holds');return
            if s['nextStartAt'] is None:
                n=s.get('restartCount',0)+1;self.save(p,s,phase='backoff',restartCount=n,nextStartAt=time.time()+min(30,.5*2**min(n-1,6)),reason='restart policy');return
            if time.time()<s['nextStartAt']:return
            should_start=True
        if current and ts is None:return
        # Recover an interrupted publication before creating another child. A child
        # never spawns until the Service's currentTask points to it.
        orphan=next((t for _,t in self.objects.values() if t['kind']=='Task' and t.get('service',{}).get('id')==d['id'] and t['service']['declarationDigest']==digest(d) and self.state(self.objects[t['id']][0]) is None),None)
        task=orphan or dict(copy.deepcopy(d['task']),kind='Task',id='t-'+uuid.uuid4().hex,publisher='owner-'+self.c['node'],node=self.c['node'],revision=1,desired='run',service=dict(id=d['id'],version=d['version'],declarationDigest=digest(d)))
        tp=publish(self.root,task,writer=self.c['node'],publishers=self.c['publishers'])
        self.objects[task['id']]=(tp,task)
        self.save(p,s,declarationDigest=digest(d),phase='starting',currentTask=task['id'],nextStartAt=None,reason=None)
    def round(self):
        self.accept()
        try:reconcile(self.c)
        except (OSError,ValueError,KeyError,TypeError) as e:self.errors.append('scheduler: '+str(e))
        for p,d in list(self.objects.values()):
            try:
                if d['kind']=='Service':self.service(p,d)
            except (OSError,ValueError,KeyError,TypeError,RuntimeError) as e:self.errors.append(d['id']+': '+str(e))
        for p,d in list(self.objects.values()):
            try:
                if d['kind']=='Task':self.task(p,d)
            except (OSError,ValueError,KeyError,TypeError,RuntimeError) as e:self.errors.append(d['id']+': '+str(e))
        record=read(self.private/'owner.json');record.update(node=self.c['node'],observedAt=now(),diagnostics=self.errors);write(self.private/'owner.json',record)
    def run(self):
        def stop(*unused):self.stopped=True
        for sig in (signal.SIGTERM,signal.SIGINT):signal.signal(sig,stop)
        while not self.stopped:self.round();time.sleep(self.c.get('interval',.2))
