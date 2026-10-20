# Shared native foreground lifecycle (P2b 68wblg; Service always 832kxv)

**Experimental, unstable API; not a production runtime/P3 or deployment PASS.**
Based on nf2q61/design §4–7, following P2a prepare. Nothing imports this from
`serviced.py`, `sync-loop.sh` or another legacy entry. No service/profile/caller,
agent adapter, scheduler, protocol, recovery mapping or fixed S→A0 dependency is
changed. There is no background coordinator, watcher, daemon, automatic startup,
network transport, distributed takeover or new orchestration platform. Every action
below is an explicit call. No model or production socket is involved.

## Supported execution contract

- Linux `/proc` or Darwin `libproc`, POSIX `flock`, `setsid`, same local UID. Each root is a private,
  local node filesystem, not a synchronized/multi-host lock. Root ownership,
  no-symlink/hardlink checks and file-only pinned Git preparation extend P2a.
- **Only self-created immutable cooperating foreground payloads.** Exec and
  ordinary FD-closing helpers are supported, provided PID/start identity,
  process group, session and UID are preserved. Descendants must stay in the
  recorded process group; no daemonization, setsid/setpgid escape or hostile
  same-UID programs. These are support preconditions, not a security sandbox.
  No per-run lease file or private payload FD exists. Generic real-exec/helper
  fixtures test this contract; actual SSH/rsync integration remains unverified.
- `Popen(argv, shell=False, start_new_session=True)` (default close_fds), stdin
  `/dev/null`, cwd `work/<kind>/<id>/<attempt>/<generation>`. stdout/stderr are
  exclusive new local files under `logs/` with the same relative identity.
  No shell interpolation; only declared `${work}`/`${code.alias}` substitutions.
- Payload environment is exactly runtime `PATH=os.defpath`, fresh work `HOME`
  and `TMPDIR`, `LC_ALL=C.UTF-8`, and explicit declared variables.
  Declared variables cannot override these runtime keys. No inherited agent,
  API-key, proxy, Python import or provider environment. argv[0] is explicitly
  resolved (declared Python uses prepare's verified current interpreter).
- Preparation/tool/input/start failures produce explicit failed facts without
  starting that payload. `prepare` still never executes argv. A finite batch is
  **not transactional**: a later operation's prepare failure does not undo a
  prior explicitly requested launch. Input files/syntax are preflighted before
  batch operations; state conflicts are checked at the operation boundary.

Additional local directories (all outside empty `protocol/`):

```
state/owners/node.lock, node.json, owner.json   stable root/node fence + epoch
state/requests/<requestId>.json                operation digest + pending/receipt
state/objects/Task/<id>/<attempt>.json         immutable declaration binding
state/objects/Service/<id>/service.json        declaration and policy head
state/runs/<kind>/<id>/<attempt>/<gen>/
  run.json, result.json                        local facts, terminal commit
work/<kind>/<id>/<attempt>/<gen>/              payload cwd, HOME and TMPDIR
logs/<kind>/<id>/<attempt>/<gen>/stdout,stderr local files, not protocol events
```

One stable local node lock is acquired nonblocking before any lifecycle writing.
`node.json` binds the root to one node name; even another node alias cannot bypass
the root lock. `ownerEpoch` identifies this invocation, not a distributed lease.
Closed/fork-inherited coordinator objects refuse further operations. Private lock
inodes are never unlinked/replaced. Files are atomically write/fsync/rename
published. The synchronous, node-lock-fenced sole writer checks that terminal
`result.json` is absent and atomically renames it into place once; subsequent
terminal publication/mutation is refused. There is no hardlink/unlink window
that could leave a valid result with multiple links after a crash. Concurrent
calls on one Runtime object are unsupported. The result is the **single terminal commit point**;
mutable `run.json` never first advertises an uncommitted terminal state. This is
process-crash publication, not a tested storage/power-loss durability guarantee.
No automatic GC/repair of unknown artifacts is implemented.

## One kernel, two lifecycle owners

`kernel.py` owns spawn, setsid, Popen handles, real wait status, Linux identity,
FD fences, observation and bounded TERM/KILL. `lifecycle.py` has independent
Task/Service state machines; it does not duplicate supervision or turn Task into
`restartPolicy=never`. Runtime never reads reports, agent envelopes, provider
outcomes, resources, scheduler needs or reaper facts.

### Task

`accepted → preparing → starting → running → succeeded|failed|canceled|lost`.
Cancellation records `canceling`; uncertain cleanup records `cleanupUnknown`,
which is blocking and **not a terminal result**. History records phase changes,
not every identical running poll. Each observation includes its actual time.

- Stable key `(Task,id,attemptId,1)`. Each attempt binds the complete declaration
  digest and requestId. Releasing it again returns existing facts, including an
  accepted-but-not-started run, and **never spawns again**. Changed digest fails.
  Request IDs are unique across operations in this node root; reuse for another
  body/operation is rejected.
- `accept_task(start=False)` supports pre-start cancellation. `start_task` is an
  explicit one-shot start of an accepted run, never replay of preparing/starting.
- Terminal attempts absorb release/start/cancel and cannot be overwritten.
  `rerun_task(id,oldAttempt,newDeclaration)` requires terminal old attempt and a
  new attempt/request, preserving old facts. A plain release of another attempt
  under an existing Task id is refused. **No Task auto retry, including lost.**
- `succeeded` only means a real `Popen.poll/wait` observed exit 0 and group
  convergence. Nonzero exits are failed; signals have `exitCode=null, signal=N`.
  Before spawn there is no exit status. Lost wait status is `lost`, both fields
  null, never guessed success from a disappeared pid or a sent signal.
- Cancellation first observes natural termination. Already observed natural
  success/failure wins, with no signal. A signal sent to a live matching target
  retains cancellation intent; canceled is published only after convergence.
  An already terminal cancel is a noop. Orphan cancellation can be canceled with
  **unknown null status**, not a fabricated wait code/signal.

### Service

Declaration identity persists independently of each run. `manual`,
`on-failure` and `always` share the same Service state machine and kernel.
Task retry/absorption semantics are unchanged. This unconnected policy addition
is not an agent auto adapter, P3b approval or a production rollout.

- Initial online declaration prepares, allocates a new generation and runs.
  `manual` does not restart after either success or failure. `on-failure` restarts
  after nonzero/signal/unknown orphan exit, but not a real observed exit 0.
  `always` restarts after both real exit 0 and nonzero/signal exits. A recorded
  orphan with **no real wait** can apply either applicable restart policy only
  after the identity/group convergence checks; its exitCode and
  signal remain null, never guessed. No policy bypasses cleanupUnknown.
- Applicable exits schedule the existing bounded exponential backoff
  `min(maxSeconds, initialSeconds*2^min(failures,32))`. The historical `failures`
  field counts scheduled restart delays, including exit 0 under `always`;
  it increments once per terminal generation and is not reset by running or a
  successful generation. New declaration versions initialize it as before.
  **Only an explicit later `tick_service` at/after nextStartAt** starts one new
  generation. Repeated pre-deadline ticks/terminal observations do not extend
  the deadline or increment it again; repeated ticks of the new running gen do
  not spawn another. Immutable old generation results are preserved.
  No sleeping supervisor, timer, background loop or daemon is installed.
- Prepare/start failure contracts are unchanged: submission prepare failure
  preserves the previous declaration/process (or leaves no Service head for a
  new failed submission); a Service head in `failed` does not retry on tick.
  `always` is not an automatic prepare repair or an unbounded startup retry.
- New version must be strictly newer when the declaration changes. Online new
  input prepares **before** stopping the old generation. Prepare failure leaves
  the previous declaration/payload unchanged. Once prepared, replacement must
  confirm old group convergence before allocating/spawning a new gen.
- Offline/stop durably sets hold before effects, clears nextStartAt and
  suppresses future automatic generations, **including always**. A historical
  backoffSeconds value is diagnostic only, not a live schedule. Repeated ticks
  and resubmitting the unchanged online declaration cannot clear hold. Explicit restart creates a generation under
  the same Service identity, not a new Service; offline cannot implicitly
  restart. New online version is an explicit reactivation. Stop does not erase
  old results or declarations.
- Repeated control request returns its original receipt, not another restart.
  `expectedGeneration` mismatch returns stale-generation noop, never signals
  the current generation. Service controls use `attemptId=service`.
- A crash may leave a request `pending`. Duplicate pending controls/service
  submissions return an explicit unknown/pending diagnostic and **do not replay
  effects**. Observe and manual resolution are the boundary, not a new durable
  command scheduler. Unindexed allocated generations also block new spawning.

## Crash, identity, groups and real wait

There is no per-run flock, lease file, leaseFd/leaseBusy fact or private FD
injection. The node/prepare locks still fence their single writers and are not
passed to payloads. Exec is not a new generation. Helpers may close FDs freely,
but must remain in the recorded group/session/UID for their entire lifetime.
This version is evaluated on fresh isolated roots; it neither migrates nor
deletes old lease artifacts or changes existing consumers' pinned versions.

- Crash after `starting`, before spawn OR before identity publication: without
  a recorded PID/group these windows cannot be distinguished. Both Task and
  Service remain cleanupUnknown/null, even after any actual child has exited.
  No process-name scan, blind kill, replay or invented terminal result. This
  intentionally replaces the old free-lease Task lost shortcut. A known local
  spawn failure without a created child still produces explicit failed facts.
  Harness readiness markers provide cleanup authority only, never runtime facts.
- Recorded survivors are checked against boot ID, pid, starttime, pgid, session
  and UID. A reopened node owner may observe and explicitly stop a strongly
  matching live leader; it has **no inherited wait result**.
- Every read-only group scan must succeed. Strong matching *live leader* is
  required at TERM and again at KILL. Unreadable/mismatched/ambiguous identity
  blocks finalization and signals. PGID alone never grants kill authority;
  this is not an atomic guarantee against hostile same-UID/PID races.
- A real poll(None) can precede a matching zombie sample. With a matching boot,
  exact local Popen and strong non-live leader, make at most one additional
  real nonblocking poll and resample leader/group. If the local Popen still has
  no wait status, remain cleanupUnknown (`owned-wait-unavailable`), even if the
  group appears empty/dead. A zombie/sample is not an exit code. No general
  unknown retry, sleep, deadline extension or new persisted state is added.
- Stop sends bounded TERM then KILL only while the leader is strongly matching.
  Signal delivery is not convergence. A live residual member blocks the run
  and any next Service generation, even if the leader really exited zero.
  No leader plus residual children means cleanupUnknown and **no blind killpg**.
  Zombies count as exited; fixture adoption/reaping is separate from runtime.
- Recorded identity/group converged plus an actual owned wait supplies the
  real result. Reopened owner without wait: Task lost/null; Service can apply
  its declared restart policy, keeping null exitCode/signal. Wait observed then
  owner crash before publication can also be lost. Never infer status from
  signals, timestamps, reports or fixture markers. Escaped descendants are
  outside the support contract; no claim to supervise arbitrary process trees.

Stop facts retain before/after identities, group members, signals,
timestamps and observation times in `stopEvidence`. Tests retain fixture term/
exit timestamps separately. Historical S34 had one immediate-live assertion
failure before passing a rerun; its root cause was not captured. This experiment
**does not erase that failure or weaken group-kill assertions**. Both successful
and failing local test artifacts are retained for timing/identity diagnosis.

## Explicit CLI and API

Only the new CLI imports lifecycle modules, and only for `execute`:

```sh
PYTHONPATH=/absolute/candidate /absolute/python -B -m runtime.cli execute \
  --root '/absolute/new isolated/node root' --node node-a \
  --operations /absolute/operations.json
```

An operation file is a finite JSON list (1..1000). Example with a real, generated
Task declaration file:

```json
[
  {"action":"release", "declaration":"/absolute/task.json"},
  {"action":"wait", "id":"demo", "attemptId":"a1", "seconds":10}
]
```

Other operations: `accept` (no spawn), `start`/`observe` (`id,attemptId`), `rerun`
(`id,oldAttempt,declaration`), `service` (`declaration,requestId`), `tick` (`id`),
`control` (`control` object). Declaration/control schemas are in README/design;
control has exactly apiVersion=`rund-control/v1alpha1`, requestId, action,
kind, id, attemptId, expectedGeneration. Task action is cancel; Service stop or
restart. All declaration/operation file paths are absolute. `wait` is bounded
(0..3600 seconds); it does not imply cancellation on timeout.

CLI emits a JSON result list; command exit 0 means the operations were handled,
**not that every Task succeeded**. Inspect each result phase/status. Invalid
input/tools/OS errors use existing `rund prepare error` diagnostic/exit 2 for
CLI compatibility. A process can intentionally outlive an invocation; exiting
CLI forfeits its wait status. Prefer the finite release+wait batch to collect a
Task's real status, and explicitly stop remaining fixtures. No wrapper should
assume this is a stable public API or a replacement for serviced.

Python callers can use `with Runtime(root,node) as r:` and the corresponding
explicit methods. No calls are scheduled automatically. A held coordinator
owns the node lock; other CLI invocations refuse admission rather than sharing
write/kill authority. The finite CLI test uses a parent-owned barrier to release
the foreground fixture while CLI retains its real Popen handle.

## Reproduce isolated tests

```sh
RUND_LIFECYCLE_OUTPUT='/absolute/new artifacts' \
  /absolute/python -B /absolute/candidate/runtime/test_lifecycle.py
# P2a unchanged; see README for AGENTD_DIR and legacy Bash3.2 dependencies.
RUND_TEST_OUTPUT='/absolute/another new artifacts' \
  /absolute/python -B /absolute/candidate/runtime/test_prepare.py
```

Lifecycle tests create their own pinned Git source from `synthetic_payload.py`,
HOME/TMPDIR/cwd/node/work/logs/locks and all payload descendants. No cleanup of
source trees or artifacts. Before any payload, the harness preflights Linux
subreaper and pidfd syscalls (x86_64/aarch64 test ABI; actual tested host x86_64).
This supports Conda Python builds missing os/signal pidfd wrappers. Cleanup uses
only known Popen or separately recorded descendant strong identities + pidfds,
not candidate stop or process-name matching. Failures snapshot identity/group/
stop evidence before cleanup. Each test asserts all recorded descendants exited
and reaped; preflight failure is a failure, not a skip. Crash windows use actual
SIGKILL and explicit starting/spawned/recorded/waited barriers; polls only wait
for these bounded barriers. Backoff uses a deterministic test clock.

Neither implementation nor tests claim macOS, cross-machine takeover, arbitrary
workloads, credential/remote Git/package execution, power-loss storage semantics,
agent/provider success, production integration or independent review PASS.

## Native platforms

Linux keeps /proc boot/start/group facts. Darwin uses proc_bsdinfo start timeval,
kern.boottime and PROC_PGRP_ONLY enumeration via libproc, including foreign group
members. Same identity/group/UID and real owner wait gates apply. Permission or
ABI errors remain unknown. Private Git caches normalize the generated init
config before reading source objects; later byte-for-byte config validation is
unchanged. Production wiring and adoption still require explicit caller inputs.

## Caller execution context and credential references

Default execution remains isolated. An explicit optional context supplies absolute
cwd/home, PATH and inherited variable names. Declarations contain names and paths,
not inherited values. credentialRefs supplies local file, optional loader argv and
required variable names; loading occurs in memory before spawn. Loader failures
do not echo captured output. Declared env overrides loaded values; reserved runtime
keys still require the explicit context interface. Generic registry conversion is
runtime/registry.py; it uses the same Owner/Runtime and never executes workloads
itself. The bootstrap owner is outside its own workload set.

File request publication uses exclusive rename on both platforms: Linux renameat2
RENAME_NOREPLACE and Darwin renamex_np RENAME_EXCL. Existing request bodies must
match exactly; no hardlink/unlink publication window or unknown-request replay.
