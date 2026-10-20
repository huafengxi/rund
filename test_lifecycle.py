"""Reconciliation failures must not start a second copy or report success."""
import contextlib
import importlib.util
import io
import json
import tempfile
from pathlib import Path
import unittest
from unittest.mock import patch

spec = importlib.util.spec_from_file_location("serviced", Path(__file__).with_name("serviced.py"))
sv = importlib.util.module_from_spec(spec)
spec.loader.exec_module(sv)


class LifecycleTests(unittest.TestCase):
    def setUp(self):
        self.output = contextlib.redirect_stdout(io.StringIO())
        self.output.__enter__()
        self.addCleanup(self.output.__exit__, None, None, None)

    def test_empty_status(self):
        with patch.object(sv, "gather", return_value=[]):
            self.assertEqual(sv.cmd_status(), 0)
            self.assertEqual(sv.cmd_status_machine(), 0)

    def test_invalid_profiles_fail_before_probing(self):
        for data in ({"cmd": ["x"]}, {"version": True, "cmd": ["x"]},
                     {"version": 1, "cmd": "echo x"},
                     {"version": 1, "cmd": ["x"], "wrapper": "typo"}):
            with self.subTest(data=data), tempfile.TemporaryDirectory() as root:
                Path(root, "a.json").write_text(json.dumps(data))
                with self.assertRaises(SystemExit):
                    sv.load_profiles(root)

    def test_external_stop_failure_keeps_metadata(self):
        with patch.object(sv, "svc_entry", return_value={"stop_cmd": ["stop"]}), \
             patch.object(sv.subprocess, "run", return_value=type("Result", (), {"returncode": 1})()), \
             patch.object(sv, "pidfile") as pidfile, \
             patch.object(sv, "metafile") as metafile:
            self.assertEqual(sv.cmd_stop_name("a"), 1)
            pidfile.assert_not_called()
            metafile.assert_not_called()

    def test_plan_offline_never_restarts(self):
        rows = [({"name": "a", "desired": "offline"}, "online", True)]
        with patch.object(sv, "_version_fields", side_effect=AssertionError):
            self.assertEqual(list(sv.sync_plan(rows))[0][2], ["stop"])

    def test_failed_stop_skips_start_but_continues_other_services(self):
        plan = [("restart a", "a", ["stop", "start"]),
                ("start b", "b", ["start"])]
        with patch.object(sv, "gather", return_value=[]), \
             patch.object(sv, "sync_plan", return_value=plan), \
             patch.object(sv, "_run_action", side_effect=[(1, "still alive"), (0, "")]) as action, \
             patch.object(sv, "cmd_status", return_value=0):
            self.assertEqual(sv.cmd_sync(), 1)
            self.assertEqual(action.call_args_list, [(('a', 'stop'),), (('b', 'start'),)])

    def test_surviving_process_keeps_metadata(self):
        entry = {"name": "a"}
        with patch.object(sv, "svc_entry", return_value=entry), \
             patch.object(sv, "pidfile_alive", return_value=12345), \
             patch.object(sv, "matched_pids", return_value=[]), \
             patch.object(sv, "_kill"), \
             patch.object(sv, "pid_alive", return_value=True), \
             patch.object(sv.time, "time", side_effect=[0, 4]), \
             patch.object(sv.time, "sleep"), \
             patch.object(sv, "pidfile") as pidfile, \
             patch.object(sv, "metafile") as metafile:
            self.assertEqual(sv.cmd_stop_name("a"), 1)
            pidfile.assert_not_called()
            metafile.assert_not_called()


if __name__ == "__main__":
    unittest.main()
