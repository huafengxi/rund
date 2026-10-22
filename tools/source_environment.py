"""Caller-declared execution context and in-memory credential loading.

Credential values never enter declarations, receipts, prepare manifests or logs.
The local caller owns paths/loaders; these are not a hostile-user sandbox.
"""
import os
from pathlib import Path
import re
import subprocess
from .source_contract import fields,require,text

KEY=re.compile(r'[A-Za-z_][A-Za-z0-9_]*\Z')

def validate_context(value):
    fields(value,(),('cwd','home','path','inheritEnv'))
    for key in ('cwd','home'):
        if key in value:
            text(value[key]);require(Path(value[key]).is_absolute(),'absolute context '+key+' required')
    if 'path' in value:text(value['path'])
    keys=value.get('inheritEnv',[])
    require(type(keys) is list,'inherited environment names must be a list')
    for key in keys:require(type(key) is str and KEY.fullmatch(key) is not None,'invalid inherited environment key')
    require(len(keys)==len(set(keys)),'unique inherited environment names required')


def validate_credentials(refs):
    require(type(refs) is list and len(refs)<=32,'credentialRefs must be a bounded list')
    for ref in refs:
        fields(ref,('file','loader','require'))
        text(ref['file']);require(Path(ref['file']).is_absolute(),'absolute credential file required')
        require(type(ref['loader']) is list,'credential loader argv required')
        for item in ref['loader']:text(item)
        require(type(ref['require']) is list,'required credential names must be list')
        for key in ref['require']:require(type(key) is str and KEY.fullmatch(key) is not None,'invalid required environment key')


def parse_env(content):
    values={}
    for line in content.splitlines():
        match=re.match(r'^\s*(?:export\s+)?([A-Za-z_]\w*)\s*=(.*)$',line)
        if not match:continue
        key,value=match.groups();value=value.strip()
        if len(value)>=2 and value[0]==value[-1] and value[0] in ('"',"'"):value=value[1:-1]
        values[key]=value
    return values


def execution_environment(spec,baseline):
    context=spec.get('context',{});env=dict(baseline)
    for key in context.get('inheritEnv',[]):
        if key in os.environ:env[key]=os.environ[key]
    if 'home' in context:env['HOME']=context['home']
    if 'path' in context:env['PATH']=context['path']
    # Load local files in memory before declared non-secret overrides, matching
    # the original services registry precedence. Never echo loader output.
    for ref in spec['credentialRefs']:
        if ref['loader']:
            try:
                result=subprocess.run(ref['loader']+[ref['file']],env=env,
                                      stdout=subprocess.PIPE,stderr=subprocess.PIPE,
                                      timeout=30)
            except (OSError,subprocess.TimeoutExpired):
                raise ValueError('credential loader unavailable') from None
            require(result.returncode==0,'credential loader failed (exit '+str(result.returncode)+')')
            try:content=result.stdout.decode('utf-8')
            except UnicodeError:raise ValueError('credential loader output is not UTF-8') from None
        else:
            try:content=Path(ref['file']).read_text()
            except (OSError,UnicodeError):raise ValueError('credential file unavailable') from None
        env.update(parse_env(content))
    env.update(spec['env'])
    missing=[key for ref in spec['credentialRefs'] for key in ref['require'] if not env.get(key)]
    require(not missing,'required environment missing: '+', '.join(sorted(set(missing))))
    return env
