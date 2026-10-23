import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from tools.prepare import build
from runtime.store import read,write

class Tools(unittest.TestCase):
    def test_fixed_source_prepare_and_reuse(self):
        b=Path(tempfile.mkdtemp(prefix='rund-prepare-')).resolve();src=b/'source';src.mkdir();(src/'main.py').write_text('print(1)\n')
        def git(*args):return subprocess.check_output(['git','-C',str(src),*args],stderr=subprocess.STDOUT).decode().strip()
        git('init','-q','--template=');git('add','--','main.py');git('-c','user.name=Fixture','-c','user.email=fixture@example.invalid','commit','-qm','fixture')
        pin=git('rev-parse','HEAD');plan={'sources':[dict(alias='code',repository=src.as_uri(),revision=pin,requires=[])]}
        build(plan,b/'artifact',b/'cache');first=read(b/'artifact/ready.json');self.assertEqual(first['sources']['code'],pin);self.assertEqual((b/'artifact/code/main.py').read_text(),'print(1)\n')
        build(plan,b/'artifact',b/'cache');self.assertEqual(read(b/'artifact/ready.json'),first)
        with self.assertRaises(ValueError):build({'sources':[]},b/'artifact',b/'cache')
    def test_prepare_failure_does_not_publish_ready(self):
        b=Path(tempfile.mkdtemp(prefix='rund-bad-prepare-')).resolve()
        with self.assertRaises((ValueError,OSError)):build({'sources':[]},b/'artifact',b/'cache')
        self.assertFalse((b/'artifact/ready.json').exists())
if __name__=='__main__':unittest.main()
