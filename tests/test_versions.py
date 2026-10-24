import copy
import os
from pathlib import Path
import socket
import sys
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
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
    def test_single_immutable_task_and_current_service(self):
        root=self.root/'shared';p=publish(root,self.d);before=p.read_bytes()
        self.assertEqual(p.name,'declaration.json')
        self.assertEqual(publish(root,self.d),p)
        with self.assertRaises(ValueError):publish(root,dict(self.d,argv=['/bin/false']))
        self.assertEqual(p.read_bytes(),before)
        service=dict(kind='Service',id='service',node='hub',argv=['/bin/true'],version=1,desired='offline',restart='manual')
        sp=publish(root,service)
        publish(root,dict(service,version=2,argv=['/bin/false']))
        self.assertEqual(read(sp)['version'],2)
        self.assertEqual(list(sp.parent.glob('declaration*.json')),[sp])
    def test_shared_service_history_does_not_block_task_publication(self):
        root=self.root/'shared'
        write(root/'services/old/declaration.json',{'kind':'Service','obsolete':'history'})
        p=publish(root,self.d)
        from runtime.protocol import objects
        self.assertEqual(objects(root)['demo'][0],p)
        self.assertEqual(set(objects(root)),{'demo'})

    def test_cancel_from_any_node_is_separate_and_irreversible(self):
        root=self.root/'shared';p=publish(root,self.d);before=p.read_bytes()
        desired(root,'demo','cancel',mode='kill')
        self.assertEqual(p.read_bytes(),before)
        self.assertEqual(read(p.with_name('cancel.json')),{'mode':'kill'})
        self.assertEqual(desired(root,'demo','cancel',mode='terminate')['cancelMode'],'kill')
        with self.assertRaises(ValueError):desired(root,'demo','run')
        # A repeated original declaration cannot erase a cancellation request.
        publish(root,self.d)
        self.assertTrue(p.with_name('cancel.json').exists())
    def test_migration_preserves_history_and_terminal_state(self):
        folder=self.root/'shared/tasks/demo'
        write(folder/'declaration.json',dict(self.d,publisher='operator',revision=1))
        write(folder/'declaration-v1.json',self.d)
        latest=dict(self.d,desired='cancel',cancelMode='kill')
        write(folder/'declaration-v10.json',latest)
        terminal=dict(kind='Task',id='demo',node='hub',phase='succeeded',waitObserved=True,exitCode=0)
        write(folder/'state.json',terminal)
        write(self.root/'local/accepted.json',{'demo':dict(path='tasks/demo/declaration-v1.json',declaration=self.d)})
        before=(folder/'state.json').read_bytes();original=(folder/'declaration-v10.json').read_bytes()
        migrate(self.root);migrate(self.root)
        self.assertEqual((folder/'state.json').read_bytes(),before)
        self.assertEqual((folder/'declaration-v10.json').read_bytes(),original)
        self.assertEqual(read(folder/'declaration.json'),self.d)
        self.assertEqual(read(folder/'cancel.json'),{'mode':'kill'})
        self.assertEqual(read(self.root/'local/accepted.json')['demo']['path'],'tasks/demo/declaration.json')
    def test_migration_collapses_checkout_versions_and_preserves_new_current(self):
        folder=self.root/'config/objects/services/service'
        d=dict(kind='Service',id='service',node='hub',argv=['/bin/true'],version=1,desired='offline',restart='manual')
        write(folder/'declaration-v1.json',d)
        write(folder/'declaration-v12.json',dict(d,version=2))
        migrate(self.root)
        self.assertEqual(read(folder/'declaration.json')['version'],2)
        self.assertFalse(list(folder.glob('declaration-v*.json')))
        write(folder/'declaration.json',dict(d,version=3))
        migrate(self.root)
        self.assertEqual(read(folder/'declaration.json')['version'],3)
    def test_sync_uses_hub_and_conventional_transport_paths(self):
        argv,env=command(self.root,'once')
        self.assertIn('hub:'+str(self.root/'hub/shared'),argv)
        self.assertIn('--once',argv)
        self.assertEqual(Path(argv[2]),Path(__file__).resolve().parents[1]/'sync/ssh-sync.py')
        self.assertFalse((self.root/'local/tools/transport').exists())
        self.assertEqual(env['SSH_SYNC_CONFIG'],str(Path.home()/'.ssh/config'))
        self.assertFalse((self.root/'config/sync.json').exists())

if __name__=='__main__':unittest.main()
