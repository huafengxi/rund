"""Exercise bundled transport in isolated subprocesses, including its CLI suites."""
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

SOURCE=Path(__file__).resolve().parents[1]/'sync'
LINUX_ONLY={'test_foreground.py','test_probe_scratch.py','test_manifest_foreground.py'}


class BundledSync(unittest.TestCase):
    pass


def case(source):
    @unittest.skipIf(source.name in LINUX_ONLY and sys.platform!='linux','requires Linux /proc and pidfd fixtures')
    def run(self):
        # Named roots retain the foreground/probe evidence produced by the suite.
        root=Path(tempfile.mkdtemp(prefix='rund-'+source.stem+'-')).resolve()
        for name in ('home','tmp','cwd'):(root/name).mkdir()
        env=dict(os.environ,HOME=str(root/'home'),TMPDIR=str(root/'tmp'),PLATFORM_WORKSPACE=str(root/'workspace'),
                 PYTHONDONTWRITEBYTECODE='1',PYTHONPATH=str(SOURCE),AGENTS_SYNC_HUB='dev',
                 PATH=str(Path(sys.executable).parent)+':/opt/homebrew/bin:/usr/local/bin:'+os.environ.get('PATH',os.defpath),
                 Y_FOREGROUND_OUTPUT=str(root/'foreground'),Y_PROBE_OUTPUT=str(root/'probe'),Y_MANIFEST_OUTPUT=str(root/'manifest'))
        log=root/'test.log'
        with log.open('wb') as output:
            result=subprocess.run([sys.executable,'-B',str(source)],cwd=root/'cwd',env=env,
                                  stdout=output,stderr=subprocess.STDOUT,timeout=180)
        self.assertEqual(result.returncode,0,str(log)+'\n'+log.read_text()[-6000:])
    return run


for source in sorted(SOURCE.glob('test_*.py')):
    setattr(BundledSync,source.stem,case(source))


if __name__=='__main__':unittest.main()
