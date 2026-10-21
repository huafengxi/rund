"""Read-only service readiness probes; never spawn or stop a managed workload."""
import os
from pathlib import Path
import subprocess
import urllib.error
import urllib.parse
import urllib.request
from .registry import expand


def online(service,config,env=None):
    probe=service.get('status')
    if probe is None:return True,''
    if 'http' in probe:
        url=probe['http']
        local=urllib.parse.urlsplit(url).hostname in ('localhost','127.0.0.1','::1')
        opener=urllib.request.build_opener(urllib.request.ProxyHandler({})) if local else urllib.request.build_opener()
        try:
            with opener.open(url,timeout=probe.get('timeout',5)):pass
            return True,url
        except urllib.error.HTTPError:return True,url
        except (OSError,ValueError):return False,'HTTP probe unavailable'
    if env is None:
        env={key:os.environ[key] for key in config.get('inheritEnv',[]) if key in os.environ}
        env.update(HOME=config['home'],PATH=config['path'])
    try:
        result=subprocess.run([expand(a,config) for a in probe['cmd']],cwd=config['workspace'],env=env,
                              stdout=subprocess.PIPE,stderr=subprocess.DEVNULL,text=True,timeout=10)
        return result.returncode==0 and probe.get('expect','') in result.stdout,'command probe'
    except (OSError,ValueError,subprocess.TimeoutExpired):return False,'command probe unavailable'
