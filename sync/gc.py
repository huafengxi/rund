#!/usr/bin/env python3
"""Hub-side cumulative deletion lists and explicitly scheduled local cleanup.

GC_AGENTS_DIR selects the authority tree; GC_WORKSPACE provides a legacy
workspace override. The conventional neutral tree is ~/.rund/hub/shared,
with AGENTS_SYNC_HUB_DIR available for an explicit standalone caller.
"""

import argparse
import grp
import json
import os
import re
import shutil
import socket
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.realpath(__file__)))
from platform_paths import workspace_root
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from delete_contract import (GC_DIR_NAME, LIST_PREFIX, GLOB_UNSAFE,
                             BARE_FAMILY_ENTRIES, PROTECTED_SYSTEM_PATHS,
                             FORCE_MARK, split_force, validate_path as validate_delete_path)

_WORKSPACE = workspace_root(__file__)

STATE_REL = os.path.join("run", "gc", "state.json")

DEFAULT_DELAY = 300.0  # seconds; see module docstring for rationale

# Iron rule (the bot/ clan's immortality was removed by user decision
# 2026-09-06): the typed
# layout subtree bot/ is NO LONGER immortal — it is deletable through
# the gc channel like task/, topic/ and queue/ (all four clans are
# ephemeral or explicitly-ruled purgeable; topic = collaboration
# containers, queue = stateless request stations).
# The tuple stays as the mechanism for listing any
# future cleanup-exempt clan (with the audited --force bypass below).
# This exemption class is enforced HUB-SIDE ONLY (the consumer trusts
# the list for it). Critical single assets stay protected via
# PROTECTED_SYSTEM_PATHS from the shared deletion contract.
FORBIDDEN_LAYOUT_DIRS = ()

# Hub neutral directory: the authoritative
# agents/ tree moved out of the participant workspace to this neutral
# path on the hub host (hub zero-originals invariant). Used by main
# resolution when present; GC_AGENTS_DIR / explicit GC_WORKSPACE win.
NEUTRAL_HUB_DIR = os.path.expanduser(os.environ.get("AGENTS_SYNC_HUB_DIR", "~/.rund/hub/shared"))

# Audit field values must stay in this alphabet (no spaces, no '/';
# the entry must stay one line and one rsync-safe path).
_FORCE_AUDIT_SAFE = re.compile(r"^[A-Za-z0-9._:@+=,-]+$")


class GcError(Exception):
    pass


def validate_entry(e):
    """Structural (no-filesystem) validation of one delete-list entry
    string as written in a list file or a schedule. Returns None when
    legal, else a human-readable reason. Enforces the iron rules and
    the gc/ self-tree guard; the ONLY legal gc/ entry is a compression
    entry (plain file entry ``gc/delete-list.NNNNNN``).

    An entry carrying the FORCE_MARK audit marker (``add --force``
    output) is exempted from the PERMISSION classes
    (FORBIDDEN_LAYOUT_DIRS and PROTECTED_SYSTEM_PATHS) — every shape
    check applies unchanged, and the audit string must be non-empty
    and alphabet-safe."""
    e, mark = split_force(e)
    if mark is not None and not _FORCE_AUDIT_SAFE.match(mark):
        return "malformed or empty force-audit marker"
    return validate_path(e, force=mark is not None)


def validate_path(e, force=False):
    """Validate the shared wire shape, then the hub-only layout policy."""
    why = validate_delete_path(e, force=force)
    if why:
        return why
    top = e.split("/", 1)[0]
    if top in FORBIDDEN_LAYOUT_DIRS and not force:
        return (f"the {top}/ layout subtree is NEVER deletable "
                "(iron rule, cleanup-exemption law; audited bypass: "
                "add --force)")
    return None


def sanitize_entry(raw, agents_dir=None, force=False):
    """Validate one CLI path argument. Returns the normalized entry
    string (directory entries carry exactly one trailing slash).
    Raises GcError with a human-readable reason on any violation.

    ``force=True`` (``add --force``) lifts the PERMISSION classes
    (FORBIDDEN_LAYOUT_DIRS and PROTECTED_SYSTEM_PATHS); every shape
    check stays. The caller attaches the FORCE_MARK audit marker
    afterwards.

    With ``agents_dir`` given (the production CLI path), an unslashed
    path that points at an EXISTING directory is auto-normalized to a
    subtree entry (``dir/``) — a file entry for a directory would make
    the consumer os.remove a directory (OSError every cycle, never
    converging bug2)."""
    e = raw.strip()
    if not e:
        raise GcError("empty path")
    why = validate_path(e, force=force)
    if why:
        raise GcError(f"{raw!r}: {why}")
    if agents_dir is not None and not e.endswith("/"):
        p = os.path.join(agents_dir, e)
        if os.path.isdir(p):
            e += "/"
    return e


def entry_to_relpath(entry):
    """Entry string -> relative path without trailing slash (the
    FORCE_MARK audit marker, if any, is stripped first)."""
    return split_force(entry)[0].rstrip("/")


def resolved_target(agents_dir, entry):
    """Entry -> resolved ABSOLUTE path (echo hardening).
    Read-only: resolves, never creates. Ledger entries are relative to the
    agents/ tree root (see the Layout section of this docstring), so the honest
    echo is agents_dir + entry, realpath'd — the hub tree is usually the neutral
    directory (data/shared/agents), NOT the caller's workspace, and a relative
    echo hides which tree the deletion will actually happen in."""
    return os.path.realpath(os.path.join(agents_dir, entry_to_relpath(entry)))


def wrong_basis_warn(agents_dir, entry):
    """One-line WARN when an entry's literal first segment is ``agents/`` — the
    signature of writing a WORKSPACE-relative path into a ledger whose basis is
    the AGENTS tree (wrong basis; 2026-09-15 instance: an operator passed
    ``agents/bot/<name>/control/`` and two control requests stayed invisible in a
    nested tree that nobody consumes). Returns None when the entry is fine.
    Warning only: validation, scheduling and deletion semantics are untouched."""
    rel = entry_to_relpath(entry)
    if rel.split("/")[0] != "agents":
        return None
    return (f"WARN: entry {entry!r} begins with a literal 'agents/' segment, but "
            f"delete-list entries are relative to the agents/ tree "
            f"({os.path.realpath(agents_dir)}) — it resolves to "
            f"{resolved_target(agents_dir, entry)}, which is almost certainly not "
            f"what you meant (wrong basis: drop the leading 'agents/' -> "
            f"{rel[len('agents/'):]!r})")


def make_force_mark(forced_by):
    """Audit marker payload for ``add --force``: who
    authorized the exempt-area deletion, from which task/host, when
    (UTC). Alphabet-safe by construction (validated upstream).
    AGENT_SELF arrives path-style (``task/<id>``, bot layout era) and
    the marker alphabet forbids ``/`` — transcribe per the fsSafeId
    convention (``/`` → ``.``)."""
    parts = [f"by={forced_by}"]
    task = os.environ.get("AGENT_SELF")
    if task:
        parts.append("task=" + task.replace("/", "."))
    parts.append(f"host={socket.gethostname()}")
    parts.append("ts=" + time.strftime("%Y-%m-%dT%H:%M:%SZ",
                                       time.gmtime()))
    return ",".join(parts)


def list_dir(agents_dir):
    return os.path.join(agents_dir, GC_DIR_NAME)


def existing_lists(agents_dir):
    """Sorted list of delete-list filenames (relative to agents/:
    'gc/delete-list.000001')."""
    d = list_dir(agents_dir)
    try:
        names = [f for f in sorted(os.listdir(d))
                 if f.startswith(LIST_PREFIX)]
    except OSError:
        names = []
    return [os.path.join(GC_DIR_NAME, f) for f in names]


# One warning per distinct pollution signature per invocation (see
# ``read_entries``): this tool is a short-lived CLI, so process scope ==
# invocation scope.
_SKIP_REPORTED = set()


def read_entries(agents_dir):
    """Union of entries across all surviving delete-list files, in
    first-seen order. Best-effort per file (unreadable file = skipped
    this round; lists are written atomically). Entries keep their
    FORCE_MARK audit marker so compression carries the
    audit trail forward; dedup is by PATH part, a marked entry
    supersedes an unmarked duplicate.

    Illegal entries are dropped here (so they cannot ride compression
    into the next authoritative list) and reported as ONE summary line,
    not one line per entry — and the same pollution is reported only
    ONCE PER INVOCATION: every ``reap``/``add``/``list`` re-reads all
    surviving lists (``list`` reads them twice), so a polluted ledger
    would otherwise repeat the same warnings several times per round.
    The samples keep it auditable, the cap keeps it quiet, and a CHANGE
    in the polluted set (new entries) is a new signature and reports
    again."""
    best = {}
    order = []
    skipped = []
    d = list_dir(agents_dir)
    for fn in sorted(os.listdir(d)) if os.path.isdir(d) else []:
        if not fn.startswith(LIST_PREFIX):
            continue
        try:
            with open(os.path.join(d, fn), encoding="utf-8") as fh:
                for line in fh:
                    e = line.strip()
                    if not e or e.startswith("#"):
                        continue
                    why = validate_entry(e)
                    if why:
                        # Forged/malformed entries (e.g. planted on a
                        # node and pushed back) must NOT ride along
                        # into the next authoritative list via
                        # compression — filter at READ time. (Entries
                        # carrying a FORCE_MARK audit marker pass here
                        # by design: legitimate forced
                        # entries must survive compression; a forged
                        # marker is the documented tradeoff, provenance
                        # stays auditable.)
                        skipped.append((e, fn, why))
                        continue
                    path, mark = split_force(e)
                    if path in best:
                        # Duplicate path: a MARKED entry supersedes an
                        # unmarked one (audit trail wins).
                        if mark and not split_force(best[path])[1]:
                            best[path] = e
                        continue
                    best[path] = e
                    order.append(path)
        except OSError:
            continue
    if skipped:
        distinct = {}
        for e, fn, why in skipped:
            distinct.setdefault(e, (fn, why))
        cap = 5
        samples = "; ".join(f"{e!r} in {fn}: {why}"
                            for e, (fn, why)
                            in list(distinct.items())[:cap])
        more = (f"; +{len(distinct) - cap} more distinct"
                if len(distinct) > cap else "")
        sig = (agents_dir, tuple(sorted(distinct)))
        if sig not in _SKIP_REPORTED:
            _SKIP_REPORTED.add(sig)
            print(f"read_entries: SKIPPED {len(skipped)} illegal "
                  f"{'entry' if len(skipped) == 1 else 'entries'} "
                  f"({len(distinct)} distinct) in the surviving lists: "
                  f"{samples}{more}", file=sys.stderr)
    return [best[p] for p in order]


def state_path(ws):
    return os.path.join(ws, STATE_REL)


def load_state(ws):
    """{'seq': int, 'schedule': {entry: due_ts}}; tolerant of a lost or
    corrupt file (``next_seq`` resumes from the on-disk lists, so a lost
    state cannot collide with a surviving list). Keys this tool no longer
    uses are dropped, so the file converges on the next save."""
    default = {"seq": 0, "schedule": {}}
    p = state_path(ws)
    try:
        with open(p, encoding="utf-8") as fh:
            st = json.load(fh)
        if not isinstance(st, dict):
            return default
        return {"seq": st.get("seq", 0),
                "schedule": st.get("schedule", {})}
    except (OSError, ValueError):
        return default


def save_state(ws, st):
    p = state_path(ws)
    os.makedirs(os.path.dirname(p), exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(p), prefix=".state.")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(st, fh, indent=1, sort_keys=True)
        os.rename(tmp, p)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def next_seq(st, agents_dir):
    """Monotone seq: max(state, max existing list on disk) + 1 — so a
    lost state file cannot collide with a surviving list."""
    mx = int(st.get("seq", 0))
    for lp in existing_lists(agents_dir):
        try:
            mx = max(mx, int(lp.rsplit(".", 1)[1]))
        except (ValueError, IndexError):
            continue
    return mx + 1


def _tag_replica(path):
    """Hub zero-originals invariant: every file gc.py
    writes onto the hub is a CONTROLLED write and must land with the
    replica group, like any synced replica. Best-effort: warn (not
    fatal) if the replica group is unavailable."""
    try:
        os.chown(path, -1, grp.getgrnam("replica").gr_gid)
    except (KeyError, OSError) as e:
        print(f"gc: warning: cannot tag {path} with the replica "
              f"group: {e}", file=sys.stderr)


def write_list(agents_dir, seq, entries):
    """Atomically write agents/gc/delete-list.<seq> (tmp+rename)."""
    d = list_dir(agents_dir)
    os.makedirs(d, exist_ok=True)
    name = f"{LIST_PREFIX}{seq:06d}"
    fd, tmp = tempfile.mkstemp(dir=d, prefix=f".{name}.")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            for e in entries:
                fh.write(e + "\n")
        final = os.path.join(d, name)
        os.rename(tmp, final)
        _tag_replica(final)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    return os.path.join(GC_DIR_NAME, name)


def resolve_agents_dir(ws):
    """Hub agents/ tree resolution: GC_AGENTS_DIR
    (explicit) > explicit GC_WORKSPACE/agents (test compat) > the
    neutral hub directory when it exists > <workspace>/agents."""
    env = os.environ.get("GC_AGENTS_DIR")
    if env:
        return os.path.abspath(env)
    if os.environ.get("GC_WORKSPACE"):
        return os.path.join(ws, "agents")
    if os.path.isdir(NEUTRAL_HUB_DIR):
        return NEUTRAL_HUB_DIR
    return os.path.join(ws, "agents")


def is_forbidden_at_delete(rel):
    """Second-line check right before any hub-local deletion (defense
    in depth). Since (review finding S1) this is
    the FULL entry validation — iron-rule prefixes AND gc/ self-tree
    AND absolute paths / '..' traversal / glob metacharacters — so a
    tampered run/gc/state.json cannot delete outside agents/."""
    return validate_entry(rel) is not None


def _list_seq(list_rel):
    """Numeric seq of a delete-list path ('gc/delete-list.000042' -> 42),
    -1 when unparsable. Numeric rather than lexicographic so the
    ordering stays correct past six digits."""
    try:
        return int(list_rel.rsplit(".", 1)[1])
    except (ValueError, IndexError):
        return -1


def reap(ws, agents_dir, now=None, verbose=True):
    """Perform every due hub-local deletion. Returns (deleted, missed).
    Never fatal: per-path errors are reported and retried next round.

    The schedule is written ONLY by ``add``, so this can never delete
    anything an operator did not authorize; a lost or reset state simply
    means nothing is due (``list`` reports the listed-but-unscheduled
    residue, and re-running ``add`` on those paths re-plans them)."""
    now = time.time() if now is None else now
    st = load_state(ws)
    sched = st.get("schedule", {})
    deleted, missed = [], []
    refused = 0
    for entry in sorted(e for e, due in sched.items() if due <= now):
        why = validate_entry(entry)
        if why:
            # Full re-validation at delete time: add refuses these, so
            # reaching here means a tampered state.json — refuse
            # loudly and DROP the entry so it cannot retry forever.
            # (FORCE_MARK-marked entries validate here by design:
            # only add --force writes them into the schedule.)
            print(f"reap: REFUSING invalid schedule entry {entry!r}: "
                  f"{why}", file=sys.stderr)
            sched.pop(entry, None)
            refused += 1
            continue
        rel = entry_to_relpath(entry)
        p = os.path.join(agents_dir, rel)
        # is_dir is judged on the marker-stripped PATH part: a forced dir
        # entry carries its audit marker AFTER the trailing slash, so
        # entry.endswith("/") would misroute it into the file branch.
        try:
            is_dir_entry = split_force(entry)[0].endswith("/")
            if os.path.islink(p):
                # Consume the link itself, never its target (review S2).
                os.remove(p)
            elif os.path.isdir(p):
                if not is_dir_entry:
                    print(f"reap: warning: {entry!r}: file entry at a "
                          "directory; removing the subtree",
                          file=sys.stderr)
                shutil.rmtree(p)
            elif os.path.lexists(p):
                if is_dir_entry:
                    print(f"reap: warning: {entry!r}: directory entry "
                          "at a plain file; removing the file",
                          file=sys.stderr)
                os.remove(p)
            deleted.append(entry)  # absent paths also converge
            sched.pop(entry, None)
        except OSError as e:
            missed.append((entry, str(e)))
    if deleted or missed or refused:
        save_state(ws, st)
    if verbose:
        for e in deleted:
            print(f"reap: deleted {e}")
        for e, why in missed:
            print(f"reap: FAILED {e}: {why}", file=sys.stderr)
    return deleted, missed


def cmd_add(ws, agents_dir, paths, delay, wait, force=False,
            forced_by=None):
    # Keep the schedule honest first: delete whatever is already due.
    reap(ws, agents_dir, verbose=True)

    # --force: user-authorized deletion of exempt areas (the
    # PERMISSION classes: FORBIDDEN_LAYOUT_DIRS and
    # PROTECTED_SYSTEM_PATHS). Entries that needed the bypass carry
    # the FORCE_MARK audit marker in the list (who/when/where forced
    # what). Regular entries are never marked.
    mark = make_force_mark(forced_by) if force else None

    entries_new = []
    for p in paths:
        try:
            e = sanitize_entry(p, agents_dir, force=force)
        except GcError as why:
            print(f"add: REJECTED {why}", file=sys.stderr)
            return 1
        if mark and validate_path(e, force=False):
            # Would have been refused without --force -> exempt-area
            # entry -> must carry the audit marker in the ledger.
            e += FORCE_MARK + mark
        # Echo hardening: show the RESOLVED absolute target before
        # anything is written, plus a wrong-basis WARN for a literal 'agents/'
        # first segment. Echo only — the entry string and the deletion semantics
        # are unchanged.
        print(f"add: entry {e!r} -> {resolved_target(agents_dir, e)}")
        w = wrong_basis_warn(agents_dir, e)
        if w:
            print(w, file=sys.stderr)
        entries_new.append(e)
    if not entries_new:
        print("add: nothing to add", file=sys.stderr)
        return 1

    st = load_state(ws)
    old_lists = existing_lists(agents_dir)
    union = read_entries(agents_dir)
    have = {}  # path part -> index in union (marker-aware dedup)
    for i, e in enumerate(union):
        have[split_force(e)[0]] = i
    for e in entries_new:
        path, new_mark = split_force(e)
        if path not in have:
            have[path] = len(union)
            union.append(e)
        elif new_mark and not split_force(union[have[path]])[1]:
            union[have[path]] = e  # marked supersedes unmarked
    for lp in old_lists:
        if lp not in have:
            have[lp] = len(union)
            union.append(lp)  # compression: new list deletes old lists

    seq = next_seq(st, agents_dir)
    written = write_list(agents_dir, seq, union)
    print(f"add: wrote agents/{written} ({len(union)} entries, "
          f"{len(entries_new)} new, compressing {len(old_lists)} old "
          f"list(s))")

    now = time.time()
    due = now + delay
    sched = st.setdefault("schedule", {})
    batch_due = []
    for e in entries_new:
        if e not in sched:
            sched[e] = due
        batch_due.append(sched[e])
    for lp in old_lists:
        if lp not in sched:
            sched[lp] = due
        batch_due.append(sched[lp])
    st["seq"] = seq
    save_state(ws, st)
    print(f"add: scheduled {len(batch_due)} hub-local deletion(s) due "
          f"in {delay:.0f}s")

    if wait:
        target = max(batch_due) if batch_due else now
        remain = target - time.time()
        if remain > 0:
            print(f"add: --wait: sleeping {remain:.0f}s until due ...")
            time.sleep(remain)
        deleted, missed = reap(ws, agents_dir, verbose=True)
        if missed:
            print(f"add: {len(missed)} deletion(s) failed (see above); "
                  "retry with `reap`", file=sys.stderr)
            return 1
    return 0


def cmd_reap(ws, agents_dir):
    deleted, missed = reap(ws, agents_dir, verbose=True)
    if not deleted and not missed:
        print("reap: nothing due")
    return 1 if missed else 0


def cmd_list(ws, agents_dir):
    # Echo hardening: pin the BASIS first (every entry below is
    # relative to it), then echo each pending entry with its resolved absolute
    # target and a wrong-basis WARN for a literal 'agents/' first segment.
    # Read-only: no state change, no grace clock, deletion semantics untouched.
    print(f"agents tree (realpath): {os.path.realpath(agents_dir)}")
    lists = existing_lists(agents_dir)
    print(f"lists ({len(lists)}):")
    for lp in lists:
        p = os.path.join(agents_dir, lp)
        try:
            n = sum(1 for ln in open(p, encoding="utf-8") if ln.strip())
        except OSError:
            n = -1
        print(f"  agents/{lp}  ({n} entries)")
    entries = read_entries(agents_dir)
    print(f"union: {len(entries)} pending path(s)")
    for e in entries:
        print(f"  {e}  -> {resolved_target(agents_dir, e)}")
        w = wrong_basis_warn(agents_dir, e)
        if w:
            print(w, file=sys.stderr)
    st = load_state(ws)
    sched = st.get("schedule", {})
    now = time.time()
    print(f"schedule ({len(sched)} pending hub-local deletion(s)):")
    for e, due in sorted(sched.items(), key=lambda kv: kv[1]):
        remain = due - now
        print(f"  {e}  due in {remain:.0f}s"
              if remain > 0 else f"  {e}  DUE NOW")
    # READ-ONLY symptom report: listed paths that are still present here
    # and have no schedule entry. Normal right after a state loss (the
    # plan is a cache); the fix is an explicit `add` of those paths, so
    # nothing here mutates the state or starts a grace clock.
    scheduled = {entry_to_relpath(k) for k in sched}
    residue = [e for e in entries
               if entry_to_relpath(e) not in scheduled
               and not entry_to_relpath(e).startswith(GC_DIR_NAME + "/")
               and os.path.lexists(os.path.join(
                   agents_dir, entry_to_relpath(e)))]
    print(f"unscheduled ({len(residue)} listed path(s) still present "
          f"here with no plan; re-run `gc.py add <path>` to schedule "
          f"them):")
    for e in residue:
        print(f"  {e}")
    return 0


def main(argv=None):
    ws = os.environ.get("GC_WORKSPACE") or _WORKSPACE
    agents_dir = resolve_agents_dir(ws)

    p = argparse.ArgumentParser(
        description="GC delete-list tool: propagate agents/ deletions "
                    "to all sync-mesh nodes via explicit cumulative "
                    "delete lists (run ON THE HUB only).")
    sub = p.add_subparsers(dest="cmd", required=True)

    sp = sub.add_parser("add", help="add paths to a new delete-list")
    sp.add_argument("paths", nargs="+", metavar="PATH",
                    help="path relative to agents/ (trailing slash = "
                         "directory subtree)")
    sp.add_argument("--delay", type=float, default=DEFAULT_DELAY,
                    metavar="SEC",
                    help=f"fixed delay before hub-local deletion "
                         f"(default {DEFAULT_DELAY:.0f}s)")
    sp.add_argument("--wait", action="store_true",
                    help="block until due and perform the hub-local "
                         "deletion before returning")
    sp.add_argument("--force", action="store_true",
                    help="AUDITED BYPASS (2026-08-31 user decision; "
                         "extended 2026-10-04 to protected assets): "
                         "accept cleanup-exempt paths "
                         "(FORBIDDEN_LAYOUT_DIRS) AND protected system "
                         "assets (PROTECTED_SYSTEM_PATHS). Every such "
                         "entry is recorded with an inline '#FORCED:' "
                         "audit marker (who/when/where) — the marker "
                         "is also the consumer-side gate for the "
                         "protected class. Shape validation unchanged "
                         "(bare family containers, gc/ self-tree, "
                         "absolute/'..'/glob stay refused). Use only "
                         "for USER-AUTHORIZED deletions.")
    sp.add_argument("--force-by", default=None, metavar="NAME",
                    help="who authorized the forced deletion "
                         "(default: $USER); stored in the audit "
                         "marker")

    sub.add_parser("reap", help="perform the due hub-local deletions "
                                "(the schedule is written by `add`)")
    sub.add_parser("list", help="show lists and pending schedule")

    args = p.parse_args(argv)

    if not os.path.isdir(agents_dir):
        print(f"gc: error: {agents_dir} not found (is GC_AGENTS_DIR / "
              f"GC_WORKSPACE correct?)", file=sys.stderr)
        return 1

    if args.cmd == "add":
        forced_by = args.force_by or os.environ.get("USER", "unknown")
        if args.force and not _FORCE_AUDIT_SAFE.match(forced_by):
            print("gc: error: --force-by must match "
                  f"{_FORCE_AUDIT_SAFE.pattern}", file=sys.stderr)
            return 1
        return cmd_add(ws, agents_dir, args.paths, args.delay,
                       args.wait, force=args.force, forced_by=forced_by)
    if args.cmd == "reap":
        return cmd_reap(ws, agents_dir)
    if args.cmd == "list":
        return cmd_list(ws, agents_dir)
    return 2


if __name__ == "__main__":
    sys.exit(main())
