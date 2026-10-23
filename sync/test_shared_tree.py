"""Shared runtime roots use the same original/replica transport and deletion contract."""
import importlib.util
from pathlib import Path
import tempfile
import threading
import time
import types
import unittest
from unittest.mock import patch
import delete_contract

spec=importlib.util.spec_from_file_location('shared_sync',Path(__file__).with_name('ssh-sync.py'))
sync=importlib.util.module_from_spec(spec);spec.loader.exec_module(sync)


class SharedTree(unittest.TestCase):
    def test_prefixed_agent_assets_and_runtime_containers(self):
        for p in ('agents/','agents/task/','services/','tasks/','configs/','agents/queue/dispatcher/'):
            self.assertIsNotNone(delete_contract.validate_path(p),p)
        self.assertIsNotNone(delete_contract.validate_path('agents/task/',force=True))
        self.assertIsNone(delete_contract.validate_path('agents/task/example/'))
        self.assertIsNone(delete_contract.validate_path('tasks/example/'))
        self.assertIsNone(delete_contract.validate_path('agents/queue/dispatcher/',force=True))
    def test_pair_lock_prevents_interleaved_cycles(self):
        root=Path(tempfile.mkdtemp(prefix='sync-cycle-'));args=types.SimpleNamespace(cycle_lock=str(root/'link.lock'))
        events=[]
        def transfer(*a):
            events.append((threading.current_thread().name,a[-2]));time.sleep(.02);return True,1
        with patch.object(sync,'watch_rsync',side_effect=transfer):
            threads=[threading.Thread(target=sync.watch_cycle,name=str(i),args=(args,'cfg','host','local','remote',1)) for i in range(2)]
            for t in threads:t.start()
            for t in threads:t.join(3);self.assertFalse(t.is_alive())
        self.assertEqual([x[1] for x in events],['push','pull','push','pull'])
        self.assertEqual(events[0][0],events[1][0]);self.assertEqual(events[2][0],events[3][0])

if __name__=='__main__':unittest.main()
