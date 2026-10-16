#!/usr/bin/env python3
"""Read-only history/projection/net-tree verifier; writes one caller output JSON.

--source is an isolated copy of the source repository with the frozen history.
--origin optionally checks the actual delivery origin's named refs and closure.
No fetch, checkout, deletion, or production service calls are performed.
"""
import argparse
import json
from pathlib import Path
import subprocess

REPO = Path(__file__).resolve().parent.parent


def git(repo, *args):
    return subprocess.check_output(["git", "-C", str(repo), *args])


def tree(repo, revision):
    entries = {}
    for line in git(repo, "ls-tree", "-r", "-z", revision).split(b"\0"):
        if not line:
            continue
        meta, path = line.split(b"\t")
        mode, kind, oid = meta.decode().split()
        assert kind == "blob", (path, kind)
        entries[path.decode()] = {"mode":mode,"oid":oid}
    return entries


def owned(entries):
    return {p.removeprefix("serviced/"):entry for p,entry in entries.items()
            if p.startswith("serviced/") or p in ("platform_paths.py", "test_platform_paths.py")}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", required=True, type=Path)
    parser.add_argument("--origin", type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    recovery = REPO / "recovery"
    mapping = json.loads((recovery / "source-map.json").read_text())
    refs = json.loads((recovery / "source-refs.json").read_text())
    adaptation = json.loads((recovery / "adaptations.json").read_text())
    net = json.loads((recovery / "net-paths.json").read_text())
    baseline, frozen = mapping["baseline"], mapping["frozen"]
    old = refs["original_main"]
    sequence = git(args.source,"rev-list","--first-parent","--reverse",baseline+".."+frozen).decode().splitlines()
    assert sequence == [r["source_commit"] for r in mapping["records"]]
    expected = tree(REPO,old)
    assert expected == owned(tree(args.source,baseline))
    parent = old
    projected = 0
    for record in mapping["records"]:
        sha = record["source_commit"]
        source_before = tree(args.source,sha+"^1")
        source_after = tree(args.source,sha)
        before, after = owned(source_before), owned(source_after)
        changes = []
        for p in sorted(before.keys() | after.keys()):
            if before.get(p) == after.get(p):
                continue
            source_path = "serviced/"+p if "serviced/"+p in source_before or "serviced/"+p in source_after else p
            changes.append({"source":source_path,"target":p,"before":before.get(p),
                            "after":after.get(p),"deleted":p not in after})
        assert changes == record["changes"], sha
        assert parent == record["target_parent"], sha
        if not changes:
            assert record["target_commit"] is None
            continue
        for change in changes:
            if change["after"] is None:
                expected.pop(change["target"])
            else:
                expected[change["target"]] = change["after"]
        commit = record["target_commit"]
        assert git(REPO,"rev-parse",commit+"^").decode().strip() == parent
        assert tree(REPO,commit) == expected, sha
        message = git(REPO,"show","-s","--format=%B",commit).decode()
        for fragment in ("Source-Repo: agent-platform", "Source-Commit: "+sha,
                         "Source-Paths: "+", ".join(c["source"] for c in changes),
                         "Target-Paths: "+", ".join(c["target"] for c in changes),
                         "TaskId: rd6-s-175748"):
            assert fragment in message, (sha,fragment)
        encoded = next(line.removeprefix("Source-Modes-Deletes: ") for line in message.splitlines() if line.startswith("Source-Modes-Deletes: "))
        assert json.loads(encoded) == changes
        parent = commit
        projected += 1
    assert parent == mapping["projection_tip"] == adaptation["projection_tip"]
    assert expected == owned(tree(args.source,frozen))
    adapted = adaptation["adaptation_commit"]
    assert git(REPO,"rev-parse",adapted+"^").decode().strip() == parent
    actual_changes = git(REPO,"diff","--name-only",parent,adapted).decode().splitlines()
    assert actual_changes == sorted(adaptation["paths"])
    for path,declared in adaptation["paths"].items():
        assert tree(REPO,parent).get(path) == declared["projected"]
        assert tree(REPO,adapted).get(path) == declared["adapted"]
        assert declared["reason"]
    delivered = {p:v for p,v in tree(REPO,"HEAD").items() if not p.startswith("recovery/")}
    assert delivered == {p:v for p,v in tree(REPO,adapted).items() if not p.startswith("recovery/")}
    source = owned(tree(args.source,frozen))
    original = tree(REPO,old)
    assert sorted(net) == sorted(original.keys() | source.keys() | delivered.keys())
    for p,declared in net.items():
        assert declared["original"] == original.get(p)
        assert declared["source"] == source.get(p)
        assert declared["projected"] == expected.get(p)
        assert declared["delivered"] == delivered.get(p)
        assert declared["deleted"] == (p not in delivered)
        if expected.get(p) != delivered.get(p):
            assert p in adaptation["paths"]
    targets = [REPO] + ([args.origin] if args.origin else [])
    closure_results = []
    for target in targets:
        git(target,"merge-base","--is-ancestor",old,"HEAD")
        for source_ref in refs["mapping"]:
            name = source_ref["target_ref"]
            if target == REPO and name != "refs/heads/main":
                name = "refs/remotes/origin/" + name.removeprefix("refs/heads/")
            oid = git(target,"rev-parse",name).decode().strip()
            if source_ref["target_ref"] == "refs/heads/main":
                git(target,"merge-base","--is-ancestor",source_ref["oid"],oid)
            else:
                assert oid == source_ref["oid"]
            source_row = next(s for s in refs["sources"] if s["label"] == source_ref["source"])
            closure = set(source_row["closures"][source_ref["source_ref"]])
            reachable = {l.split()[0] for l in git(target,"rev-list","--objects",oid).decode().splitlines()}
            assert closure <= reachable
            result = subprocess.run(["git","-C",str(target),"cat-file","--batch-check"],
                                    input="\n".join(closure)+"\n",text=True,capture_output=True,check=True)
            assert not any(" missing" in l for l in result.stdout.splitlines())
            closure_results.append({"target":str(target),"ref":name,"original_oid":source_ref["oid"],"objects":len(closure)})
        git(target,"fsck","--full","--strict")
    result = {"PASS":True,"frozen":frozen,"source_records":len(sequence),"projected":projected,
              "no_op":len(sequence)-projected,"net_paths":len(net),"closures":closure_results}
    args.output.write_text(json.dumps(result,indent=2)+"\n")
    print(json.dumps(result,indent=2))


if __name__ == "__main__":
    main()
