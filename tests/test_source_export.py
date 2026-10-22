"""Real Git fixtures for direct, atomic source preparation; no payload execution."""
import copy
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from tools.prepare import build
from tools.source_plan import validate

class Export(unittest.TestCase):
    def setUp(self):
        self.base=Path(tempfile.mkdtemp(prefix='sources-')).resolve();self.src=self.base/'repository';self.src.mkdir()
        self.git('init','-q','--template=');(self.src/'data').write_bytes(b'\x00\xff\n');(self.src/'run').write_text('#!/bin/sh\nexit 99\n');(self.src/'run').chmod(0o755)
        self.commit();self.pin=self.git('rev-parse','HEAD');self.plan=dict(format='git-sources/v1',sources=[dict(alias='main',repository=self.src.as_uri(),revision=self.pin,requires=[],lockFiles=['data'])]);self.output=self.base/'artifact';self.cache=self.base/'cache'
    def git(self,*args):return subprocess.check_output(['git','-C',str(self.src),*args],stderr=subprocess.STDOUT).decode().strip()
    def commit(self):
        self.git('add','--','.');self.git('-c','user.name=Fixture','-c','user.email=fixture@example.invalid','commit','-qm','fixture')
    def build(self,plan=None):return build(plan or self.plan,self.output,self.cache)
    def test_pin_ignores_dirty_worktree_and_modes(self):
        (self.src/'data').write_text('dirty');self.build();self.assertEqual((self.output/'main/data').read_bytes(),b'\x00\xff\n');self.assertEqual((self.output/'main/run').stat().st_mode&0o777,0o555)
    def test_reuse_and_no_duplicate_export_tree(self):
        first=self.build();self.assertEqual(self.build(),first);self.assertEqual(set(p.name for p in self.cache.iterdir()),{'git','git-home','locks'});self.assertEqual(set(p.name for p in self.output.iterdir()),{'main','ready.json'})
    def test_existing_artifact_tampering_refused(self):
        self.build();p=self.output/'main/data';p.chmod(0o600);p.write_text('changed')
        with self.assertRaises(ValueError):self.build()
    def test_incomplete_output_never_replayed(self):
        self.output.mkdir();(self.output/'evidence').write_text('retain')
        with self.assertRaises(ValueError):self.build()
        self.assertEqual((self.output/'evidence').read_text(),'retain');self.assertFalse((self.output/'ready.json').exists())
    def test_wrong_pin_no_ready(self):
        self.plan['sources'][0]['revision']='0'*40
        with self.assertRaises(ValueError):self.build()
        self.assertFalse((self.output/'ready.json').exists())
    def test_plan_rejects_execution_fields_and_bad_sources(self):
        for modify in (lambda p:p.update(argv=['bad']),lambda p:p.update(sources=[]),lambda p:p['sources'][0].update(revision='HEAD'),lambda p:p['sources'][0].update(alias='../x'),lambda p:p['sources'][0].update(requires=['missing']),lambda p:p['sources'][0].update(lockFiles=['../x'])):
            plan=copy.deepcopy(self.plan);modify(plan)
            with self.assertRaises(ValueError):validate(plan)
    def test_missing_lockfile_no_ready(self):
        self.plan['sources'][0]['lockFiles']=['missing']
        with self.assertRaises(ValueError):self.build()
        self.assertFalse((self.output/'ready.json').exists())
    def test_symlink_and_gitlink_and_lfs_rejected(self):
        for mode in ('symlink','gitlink','lfs'):
            with self.subTest(mode=mode):
                if mode=='symlink':(self.src/'unsupported').symlink_to('data');self.git('add','--','unsupported')
                elif mode=='gitlink':self.git('update-index','--add','--cacheinfo','160000,'+self.pin+',unsupported')
                else:(self.src/'unsupported').write_text('version https://git-lfs.github.com/spec/v1\noid sha256:'+'0'*64+'\nsize 1\n');self.git('add','--','unsupported')
                self.git('-c','user.name=Fixture','-c','user.email=fixture@example.invalid','commit','-qm',mode)
                plan=copy.deepcopy(self.plan);plan['sources'][0]['revision']=self.git('rev-parse','HEAD')
                with self.assertRaises(ValueError):self.build(plan)
                self.git('rm','-f','--','unsupported');self.git('-c','user.name=Fixture','-c','user.email=fixture@example.invalid','commit','-qm','remove fixture')
    def test_source_config_filters_hooks_and_global_config_not_executed(self):
        mark=self.base/'MUST_NOT_EXECUTE';command='touch '+str(mark)
        (self.src/'.gitattributes').write_text('data filter=bait\n');self.commit();self.plan['sources'][0]['revision']=self.git('rev-parse','HEAD')
        self.git('config','filter.bait.smudge',command);self.git('config','filter.bait.required','true');self.git('config','core.fsmonitor',command)
        (self.src/'.git/config').write_text('[include]\n path=/does/not/exist\ninvalid config\n')
        self.build();self.assertFalse(mark.exists())
    def test_cache_config_and_alternates_refused(self):
        self.build();db=next((self.cache/'git').iterdir());(db/'config').write_text('invalid config')
        other=self.base/'other'
        with self.assertRaises(ValueError):build(self.plan,other,self.cache)
        (self.src/'.git/objects/info/alternates').write_text('/does/not/exist')
        with self.assertRaises(ValueError):build(self.plan,self.base/'alternate',self.base/'cache2')
    def test_annotated_tag_and_blob_are_not_commit_pins(self):
        self.git('-c','user.name=Fixture','-c','user.email=fixture@example.invalid','tag','-am','fixture','tag')
        for ref in ('tag','HEAD:data','HEAD^{tree}'):
            plan=copy.deepcopy(self.plan);plan['sources'][0]['revision']=self.git('rev-parse',ref)
            with self.assertRaises(ValueError):self.build(plan)

if __name__=='__main__':unittest.main()
