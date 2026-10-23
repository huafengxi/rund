import copy
import os
from pathlib import Path
import socket
import tempfile
import unittest
from unittest.mock import patch
from runtime.config import discover,publish,node_config
from runtime.store import read,write
from runtime.protocol import desired
from tools.migrate import migrate
from tools.sync import command


class Versions(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name).resolve();(self.root/'config').mkdir()
        (self.root/'config/host-id').write_text('# hub first\n\n'+socket.gethostname()+' hub\nother-host other\n')
        self.d=dict(kind='Task',id='demo',node='hub',desired='run',argv=['/bin/true'])
    def test_host_id_alone_derives_node_hub_and_paths(self):
        c=node_config(self.root)
        self.assertEqual((c['node'],c['hub']),('hub','hub'))
        self.assertEqual(c['sharedRoot'],str(self.root/'shared'))
        self.assertNotIn('publishers',c);self.assertNotIn('admission',c)
        self.assertFalse((self.root/'config/rund.json').exists())
        with patch('socket.gethostname',return_value='other-host'):
            self.assertEqual(node_config(self.root)['hub'],'hub')
        with (self.root/'config/host-id').open('a') as f:f.write('third-host other\n')
        with self.assertRaisesRegex(ValueError,'duplicate node'):node_config(self.root)
    def test_numeric_versions_and_immutable_history(self):
        root=self.root/'shared';p=publish(root,self.d);before=p.read_bytes()
        # Full snapshots need not arrive sequentially.
        publish(root,dict(self.d,desired='cancel'),revision=2)
        p10=publish(root,dict(self.d,desired='cancel'),revision=10)
        self.assertEqual(p10.name,'declaration-v10.json')
        self.assertEqual(discover(root)[0]['demo'][0],p10)
        self.assertEqual(p.read_bytes(),before)
        with self.assertRaises(ValueError):publish(root,dict(self.d,desired='run'),revision=10)
        self.assertEqual(publish(root,dict(self.d,desired='cancel')),p10)
    def test_cancel_from_any_node_appends_without_routing(self):
        root=self.root/'shared';p=publish(root,self.d)
        desired(root,'demo','cancel',mode='kill')
        self.assertEqual(read(p),self.d)
        self.assertEqual(read(p.with_name('declaration-v2.json'))['desired'],'cancel')
    def test_migration_preserves_legacy_bytes_and_terminal_state(self):
        folder=self.root/'shared/tasks/demo';legacy=dict(self.d,publisher='operator',revision=4)
        write(folder/'declaration.json',legacy)
        terminal=dict(kind='Task',id='demo',node='hub',phase='succeeded',waitObserved=True,exitCode=0)
        write(folder/'state.json',terminal)
        write(self.root/'local/accepted.json',{'demo':dict(path='tasks/demo/declaration.json',declaration=legacy)})
        before=(folder/'state.json').read_bytes();original=(folder/'declaration.json').read_bytes()
        migrate(self.root);migrate(self.root)
        self.assertEqual((folder/'state.json').read_bytes(),before)
        self.assertEqual((folder/'declaration.json').read_bytes(),original)
        self.assertEqual(read(folder/'declaration-v4.json'),self.d)
        self.assertEqual(read(self.root/'local/accepted.json')['demo']['path'],'tasks/demo/declaration-v4.json')
    def test_sync_uses_hub_and_conventional_transport_paths(self):
        tool=self.root/'local/tools/transport/sync/ssh-sync.py';tool.parent.mkdir(parents=True);tool.write_text('')
        argv,env=command(self.root,'once')
        self.assertIn('hub:'+str(self.root/'hub/shared'),argv)
        self.assertIn('--once',argv)
        self.assertEqual(env['SSH_SYNC_CONFIG'],str(Path.home()/'.ssh/config'))
        self.assertFalse((self.root/'config/sync.json').exists())

if __name__=='__main__':unittest.main()
