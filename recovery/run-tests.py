#!/usr/bin/env python3
"""Enumerate all repository test_*.py in isolated, retained, self-created trees.

Requires a caller-provided existing Bash3.2 and a pinned agentd checkout.
Does not clone/fetch, install, run make, or invoke real service lifecycle.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile

REPO = Path(__file__).resolve().parent.parent


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, help="new independent directory (must not exist)")
    args = parser.parse_args()
    if args.output:
        root = args.output.expanduser().absolute()
        if root == REPO or REPO in root.parents:
            parser.error("test output must be independent of the source checkout")
        root.mkdir(parents=True, exist_ok=False)
    else:
        root = Path(tempfile.mkdtemp(prefix="serviced-validation-")).resolve()
    for name in ("home", "tmp", "logs"):
        (root / name).mkdir()
    agentd = Path(os.environ.get("AGENTD_DIR", REPO.parent / "agentd")).expanduser().resolve()
    pin = json.loads((REPO / "recovery/dependencies.json").read_text())["agentd"]
    head = subprocess.check_output(["git", "-C", str(agentd), "rev-parse", "HEAD"], text=True).strip()
    if head != pin["revision"]:
        raise SystemExit(f"agentd checkout must be pinned to {pin['revision']}, got {head}")
    digest = hashlib.sha256((agentd / "envscrub.py").read_bytes()).hexdigest()
    if digest != pin["envscrub_sha256"]:
        raise SystemExit("agentd envscrub differs from pinned revision")
    bash = Path(os.environ["BASH32"]).expanduser().resolve()
    version = subprocess.check_output([str(bash), "-c", 'printf "%s" "$BASH_VERSION"'], text=True)
    if not version.startswith("3.2."):
        raise SystemExit(f"BASH32 must be Bash3.2, got {version}")
    env = {"HOME":str(root / "home"), "TMPDIR":str(root / "tmp"),
           "PATH":str(Path(sys.executable).parent)+os.pathsep+os.defpath,
           "LANG":"C.UTF-8", "PYTHONDONTWRITEBYTECODE":"1",
           "AGENTD_DIR":str(agentd), "BASH32":str(bash)}
    tests = sorted(p for p in REPO.rglob("test_*.py") if ".git" not in p.parts)
    results = []
    for i, test in enumerate(tests):
        name = str(test.relative_to(REPO))
        log = root / "logs" / f"{i:02d}-{test.name}.log"
        command = [sys.executable, "-B", str(test)]
        with log.open("w") as output:
            try:
                rc = subprocess.run(command, env=env, cwd=root, stdout=output,
                                    stderr=subprocess.STDOUT, timeout=120).returncode
            except subprocess.TimeoutExpired:
                rc = 124
        record = {"test":name,"command":command,"returncode":rc,"log":str(log)}
        results.append(record)
        print(json.dumps(record), flush=True)
    data = {"agentd_revision":head,"envscrub_sha256":digest,"bash_version":version,
            "root":str(root),"tests":results,"all_passed":all(r["returncode"] == 0 for r in results)}
    (root / "results.json").write_text(json.dumps(data,indent=2)+"\n")
    print("retained_root="+str(root),flush=True)
    return 0 if data["all_passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
