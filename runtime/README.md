# rund preparation + Linux lifecycle experiment — opt-in, not production

This directory is a **non-stable sandbox contract**, `rund/v1alpha1`.
Nothing imports it from `serviced.py` or `sync-loop.sh`. It does not change the
legacy registry or its fixed agentd dependency. `validate`/`prepare` never start
payloads. P2b adds explicitly called **Linux foreground synthetic** execution;
see [LIFECYCLE.md](LIFECYCLE.md) for its separate restricted contract and CLI.
There is no daemon, automatic reconcile, adapter, deployment or production
activation. Invoke explicitly with Python 3 on POSIX (`flock` and
`O_NOFOLLOW` are required); testing is Linux only.
No third-party Python packages, agentd checkout or complete workspace is needed
for preparation.

```sh
# From the candidate checkout, or set PYTHONPATH to its absolute path.
python -B -m runtime.cli validate --declaration /absolute/declaration.json
python -B -m runtime.cli prepare --declaration /absolute/declaration.json \
  --root '/absolute/new sandbox/node root'
```

`prepare --root` is mandatory, absolute and independent of the source checkout
and repositories. There is no HOME/workspace default or fallback to a branch,
developer worktree, old source or production cache. `validate` only parses the
declaration; it does not verify objects, tools or filesystem permissions.
Both commands emit JSON on success; malformed/unsupported inputs, tool/Git/IO
failures emit `rund prepare error` to stderr and exit 2. Argument-parser errors
also exit 2. Successful prepare emits key/cacheHit/manifest/code/work paths,
not a run result or lifecycle success. The separate `execute` command is opt-in.

## Frozen experimental JSON subset

JSON only, UTF-8. Unknown fields at every object level, duplicate JSON keys,
nonfinite numbers, unsupported versions/kinds and incorrect types fail. Every
field below is required except `sources[].lockFiles`. There are no implicit
field aliases or shell evaluation.

```json
{
  "apiVersion": "rund/v1alpha1",
  "kind": "Task",
  "metadata": {"id": "demo-task", "version": 1},
  "spec": {
    "node": "node-a",
    "attemptId": "attempt-001",
    "requestId": "request-001",
    "sources": [{
      "alias": "main",
      "repository": "file:///absolute/self-created-synthetic-repository",
      "revision": "REPLACE_WITH_ACTUAL_40_LOWERCASE_HEX_COMMIT",
      "requires": [],
      "lockFiles": ["lock.txt"]
    }],
    "prepare": [],
    "toolchain": {"python": "python3"},
    "argv": ["python3", "${code.main}/demo.py", "${work}"],
    "cwd": "${work}",
    "env": {},
    "credentialRefs": []
  }
}
```

This is a shape example, **not a valid revision fixture**. The standalone test
below actually creates repositories, commits files and writes `service.json`
and `task.json` with real pins. For a Service, replace `attemptId`/`requestId`
with both:

```json
"desired": "online",
"restart": {"policy": "on-failure", "initialSeconds": 1, "maxSeconds": 8}
```

Rules:

- `metadata.id`, `spec.node`, source aliases and Task attempt/request IDs match
  `[A-Za-z0-9][A-Za-z0-9_-]{0,63}`. Metadata version is a positive integer, not
  bool. Node is a declaration identity only; prepare does not route to a host.
- Service accepts desired `online|offline`, policy `manual|on-failure|always`,
  numeric backoff `0 <= initialSeconds <= maxSeconds <= 86400`. Task has its
  **own required attempt/request identity and rejects Service restart fields**.
  These are schema/prepare capabilities, not proof of lifecycle support:
  execution rejects `always`; only `manual`/`on-failure` are implemented.
  A prepare-key is not a Task execution/attempt ledger.
- 1..64 sources. Aliases must be unique even under case folding. `requires`
  contains exact source aliases, without duplicates, self-dependencies or
  cycles. This is not scheduler resources/needs. With empty preparation there
  is no source script or dependency action to order.
- Repository is a canonical absolute `file:///` URI, percent-encoded as by
  `Path.as_uri()`, no authority/credentials/query/fragment. The caller must use
  **only its own synthetic repositories** for this experiment; there is no
  embedded host/repository/credential allowlist. Bare and normal `.git`
  directories work; gitdir files, linked worktrees, alternate object stores and
  symbolic links in source ancestry/object stores are unsupported. Source and
  node root must be disjoint by path components, not string prefixes.
- Revision is exactly a lowercase 40hex SHA-1 **commit object**. Branch/tag names,
  abbreviated pins, tree/blob/annotated-tag objects, missing/corrupt objects and
  non-SHA1 object identities fail. A full commit ID remains valid irrespective
  of whether some external branch/tag happens to point at that commit.
- Optional `lockFiles` lists unique, tracked **regular files** relative to that
  source. No absolute paths, empty components, `.`, `..`, backslashes or `.git`
  components. SHA-256 of their pinned bytes enters the key. These files are
  inputs, not package-manager support; missing files/directories are errors.
- `prepare` must be `[]`; nonempty steps are rejected, not ignored.
  `credentialRefs` must be `[]`; no credentials are resolved or loaded.
- The only toolchain key is `python`. Its executable must resolve to the very
  interpreter running this command; missing/other interpreters are refused
  without invoking them. Python may be an absolute path or PATH-resolved name.
  Git must be on PATH. These are operator-supplied trusted tools, **not tools
  selected or installed by the source repository**.
- `argv` is a nonempty list of nonempty NUL-free strings. `cwd` must be exactly
  `${work}`. Env is string values keyed by shell-style identifiers. Recognized
  `${...}` placeholders are only `${work}` and `${code.<known alias>}`. They
  are validated but **not expanded or executed by prepare**. Other plain
  strings (including shell-looking arguments) remain inert data. Env values
  are declarations, not inherited credentials or environment loading.

## Filesystem / Git / publication contract

A new empty root, or a root with our exact versioned marker, is required.
Existing unrelated nonempty roots are refused. Root initialization uses a
stable local lock. Relevant ancestors/managed directories must not be symbolic
links. No automatic deletion or garbage collection occurs, including after
errors and cache hits. Private files/locks reject hardlinks and nonregular files.
The caller owns the root and supplies immutable synthetic source objects;
cooperating preparers are serialized, **hostile same-UID writers racing checks
are not supported**. This is not a sandbox for running untrusted Git binaries.

```
root/
  .rund-root.json, .init.lock       local ownership/version and initialization
  cache/<repository-key>/          node-private bare object cache, no source config
  code/<prepare-key>-<unique>/     validated read-only source snapshot(s)
  manifests/<prepare-key>.json     local complete preparation record
  work/                           separate writable payload workspace
  state/
    locks/                        per-repository and per-prepare-key flock files
    staging/<unique>/             private pre-publication / retained interrupted data
    git-home/                     synthetic Git HOME, no credential files loaded
  protocol/                       empty; no cache/code/manifest/lock/credentials copied
```

Only `protocol/` is a potential future synchronization root, **not the node
root**. No synchronizer is implemented or started. No protocol result is written.
Source/manifest paths and tool identities are local, never protocol data. Do not
put credentials in argv/env declarations; credential references/loading are
unsupported rather than secretly looked up.

Git is run with a new allowlisted environment (no inherited identity, API keys,
Git overrides, SSH agent, proxy or credential state), isolated HOME, disabled
system/global config, hooks, attributes and transports. No source repository is
used as `GIT_DIR` or process cwd. Instead, only its validated `objects/` is read
via `GIT_OBJECT_DIRECTORY` while the **node-private, exact-config** bare cache is
`GIT_DIR`. The cache configuration is checked before use. The implementation
never fetches, invokes upload-pack/remote helpers, runs checkout, applies a
filter, resolves source refs, loads source config/includes, or executes hooks.
Commit/tree/blob bytes are obtained with cat-file; their type, size and SHA-1
object hash are verified, copied into the private cache via hash-object, then
read back from that cache to detect preexisting corruption. Parent history is
not fetched/copied: this is selected-commit export, not a full Git mirror.

Trees are parsed directly. Only UTF-8 safe names, ordinary files and directories
are exported. Symlinks, gitlinks/submodules, `.gitmodules`, `.lfsconfig`, LFS
pointers, conflicting case-folded names and special modes fail. `.gitattributes`
may be inert source bytes; it is never evaluated. Export budgets are 32 MiB per
object, 128 MiB total regular-file bytes, 10,000 entries and 64 directory levels.
No LFS/submodule/filter/package/hook/payload code is run.

Manifest records original Git modes and object IDs plus regular-file SHA-256.
Read-only export projects Git `100644 -> 0444`, `100755 -> 0555`; executable bits
are preserved while all write bits are removed. Directories become 0555 before
the complete manifest is published. This is integrity/isolation, not an OS
security boundary against the owning UID. The CLI's work root is writable and
separate; nothing ever installs into the source or copies work data back.

Prepare-key includes the complete declaration (hence every full source pin,
repository identity, kind/ID/attempt/request/argv/env/etc.), preparation format,
actual Python and Git real paths + binary SHA-256 + actual versions, OS/arch,
and supported lock-file hashes. There is no undeclared package tool support or
claim of cross-machine bit reproducibility.

Per-repository locks are acquired in sorted order across the whole export;
per-key publication is also locked. Cache initialization uses a unique staging
bare directory and atomic rename. Source export is validated in staging, sealed
and atomically renamed to a unique code directory; **complete manifest is
published last**, by write/fsync/rename. Only that exact complete manifest and
revalidated content/modes/entries constitute a hit. Repeated calls still verify
source objects and the published snapshot: `cacheHit=true` means reuse of the
same complete publication, not omission of validation or offline availability.
The source repository must still exist. Lock inode names are stable, never
unlinked on success.

An interrupted staging directory, cache initialization temporary directory or
orphan code directory has no complete manifest: it is retained and a later call
can publish a fresh complete result. A partial/invalid **final manifest**,
unknown final cache or corrupted already-published code/cache is refused and
retained, not silently overwritten or repaired. Select a fresh self-owned root
or have its owner diagnose it; no auto-GC or cleanup authority is implied.
Two preparers of the same input return one publication, one miss then one hit.
Atomicity covers cooperating processes/process interruption, not a tested claim
of arbitrary storage/power-loss or malicious filesystem fault tolerance.

## Reproducible retained tests

```sh
# Output must not already exist. No automatic cleanup.
RUND_TEST_OUTPUT='/absolute/new test artifacts' \
  /absolute/python -B /absolute/checkout/runtime/test_prepare.py
```

The test runner creates its own HOME/TMPDIR/unrelated cwd, synthetic repositories
and harmless boundary baits, saves full subprocess commands/environment/rc/stdout/
stderr in `commands.jsonl`, and retains generated Service/Task declarations.
It exercises actual CLI invocations, true pins/content/modes, empty protocol,
no payload flag, wrong pins/noncommit/types/cycles/aliases, lockfiles/tool errors,
permissions, malformed artifacts, a killed preparer, cooperating concurrency,
source config/hooks/attributes and symbolic-link/containment baits.
The killed-preparer test monkeypatches a wait **only in its test subprocess**
after the first imported blob, then SIGKILLs it; no production fault-injection
flag or daemon is provided. chmod permission testing is explicitly skipped
under uid 0, never represented as a passed permission failure.

Preparation tests have no agentd dependency. The extra legacy-loader rejection
test requires `AGENTD_DIR` to point to the repository's **unchanged pinned**
dependency; without it that one test explicitly skips. Legacy tests remain at
the repository root and use their existing agentd/Bash3.2 input contracts.
No legacy test, source, recovery mapping, dependency pin, service profile,
Makefile/caller or startup path is changed by this experiment.

## Explicit execution and next boundary

P2a preparation remains unchanged. P2b's [Linux lifecycle contract](LIFECYCLE.md)
is an additional, explicitly invoked experiment, not a production runtime.
SSH/HTTPS, credentials, package preparation, nonempty steps, LFS/submodules,
Task automatic retry, daemon/reconcile, arbitrary/daemonized payloads, agent
adapter, deployment, production activation and automatic migration guards are
not provided. A successful prepare, process exit 0 or test exit 0 is not an agent
report/provider result, independent review PASS or permission to proceed through
an execution DAG.
