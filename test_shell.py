"""Real Linux Bash3.2 shell/root/second-entry regression; synthetic sync only.

BASH32 must point to an existing Bash3.2 binary, never a newer substitute.
No download/build and no real registry/service start or stop occurs.
"""
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time
import unittest

HERE = Path(__file__).resolve().parent


class ShellTests(unittest.TestCase):
    def test_complete_loop_root_and_second_python_entry(self):
        bash = Path(os.environ["BASH32"]).resolve()
        version = subprocess.check_output([str(bash), "-c", 'printf "%s" "$BASH_VERSION"'], text=True)
        self.assertTrue(version.startswith("3.2."), version)
        subprocess.run([str(bash), "-n", str(HERE / "sync-loop.sh")], check=True)
        root = Path(tempfile.mkdtemp(prefix="serviced-bash32-")).resolve()
        cases = []
        for entry_kind in ("real", "relative", "absolute-link", "relative-link", "chain"):
            for override in (None, "", "absolute", "relative", "tilde"):
                case = root / (entry_kind + "-" + str(override))
                case.mkdir()
                workspace = case / "default workspace"
                repo = workspace / "source checkout with spaces"
                repo.mkdir(parents=True)
                for name in ("sync-loop.sh", "platform_paths.py"):
                    shutil.copy2(HERE / name, repo / name)
                # Never run the real serviced lifecycle. The exact shell's second
                # Python entry must resolve to this local synthetic observer.
                (repo / "serviced.py").write_text('''import json,os,pathlib,sys
assert sys.argv[1:]==["sync"]
p=pathlib.Path(os.environ["OBSERVATION"])
p.write_text(json.dumps({"cwd":os.getcwd(),"argv":sys.argv,"source":str(pathlib.Path(__file__).resolve()),"bash":os.environ["FIXTURE_BASH_VERSION"]}))
print("synthetic sync action; no service invoked",flush=True)
raise SystemExit(7)
''')
                alias = case / "shell alias.sh"
                alias.symlink_to(repo / "sync-loop.sh" if entry_kind == "absolute-link"
                                 else os.path.relpath(repo / "sync-loop.sh", case))
                chain = case / "chain alias.sh"
                chain.symlink_to(alias.name)
                entry = {"real":str(repo / "sync-loop.sh"),
                         "relative":os.path.relpath(repo / "sync-loop.sh", case),
                         "absolute-link":str(alias), "relative-link":alias.name,
                         "chain":chain.name}[entry_kind]
                bindir = case / "bin"
                bindir.mkdir()
                sleep = bindir / "sleep"
                sleep.write_text("#!" + sys.executable + "\nimport os,pathlib,time\npathlib.Path(os.environ['SLEEP_MARK']).touch()\ntime.sleep(30)\n")
                sleep.chmod(0o755)
                observe = case / "observed.json"
                sleep_mark = case / "sleeping"
                bashlog = case / "bash-versions"
                bashenv = case / "bashenv"
                bashenv.write_text('case "$BASH_VERSION" in 3.2.*) ;; *) exit 91;; esac\n'
                                   'export FIXTURE_BASH_VERSION="$BASH_VERSION"\n'
                                   'printf "%s\\n" "$BASH_VERSION" >> "$BASH_VERSION_LOG"\n')
                env = {"HOME":str(case), "PATH":str(bindir)+os.pathsep+str(Path(sys.executable).parent)+os.pathsep+os.defpath,
                       "OBSERVATION":str(observe), "SLEEP_MARK":str(sleep_mark),
                       "BASH_ENV":str(bashenv), "BASH_VERSION_LOG":str(bashlog),
                       "SERVICED_SYNC_INTERVAL":"99", "SERVICED_SYNC_BEAT":"99",
                       "PYTHONDONTWRITEBYTECODE":"1"}
                expected = workspace
                if override is not None:
                    value = {"":"", "absolute":str(case / "override workspace"),
                             "relative":"relative workspace", "tilde":"~/tilde workspace"}[override]
                    env["PLATFORM_WORKSPACE"] = value
                    if value:expected = case / ("override workspace" if override == "absolute" else
                                               "relative workspace" if override == "relative" else "tilde workspace")
                expected.mkdir(parents=True, exist_ok=True)
                log = case / "loop.log"
                with log.open("w") as output:
                    p = subprocess.Popen([str(bash), entry], cwd=case, env=env,
                                         stdout=output, stderr=subprocess.STDOUT)
                    try:
                        deadline = time.monotonic()+8
                        while not sleep_mark.exists() and p.poll() is None and time.monotonic()<deadline:
                            time.sleep(0.02)
                        self.assertTrue(sleep_mark.exists(), log.read_text())
                    finally:
                        if p.poll() is None:p.terminate()
                        p.wait(timeout=5)
                self.assertEqual(p.returncode, 0, log.read_text())
                obs = json.loads(observe.read_text())
                self.assertEqual(obs["cwd"], str(expected))
                self.assertEqual(obs["source"], str(repo / "serviced.py"))
                self.assertTrue(obs["bash"].startswith("3.2."), obs)
                versions = bashlog.read_text().splitlines()
                self.assertTrue(versions and all(v.startswith("3.2.") for v in versions))
                self.assertIn("rc=7（", log.read_text())  # multibyte interpolation regression
                self.assertIn("synthetic sync action", log.read_text())
                self.assertTrue((expected / "run").is_dir())
                cases.append({"entry":entry_kind,"override":override,"observation":obs,"bash_versions":versions})
        (root / "results.json").write_text(json.dumps(cases, indent=2))
        print(f"Bash3.2 complete loop cases={len(cases)} retained_root={root}")


if __name__ == "__main__":
    unittest.main()
