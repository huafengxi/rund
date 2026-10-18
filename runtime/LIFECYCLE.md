# Explicit Linux foreground lifecycle sandbox (P2b 68wblg; Service always 832kxv)

**Experimental, unstable API; not a production runtime/P3 or deployment PASS.**
Based on nf2q61/design §4–7, following P2a prepare. Nothing imports this from
`serviced.py`, `sync-loop.sh` or another legacy entry. No service/profile/caller,
agent adapter, scheduler, protocol, recovery mapping or fixed S→A0 dependency is
changed. There is no background coordinator, watcher, daemon, automatic startup,
network transport, lease takeover or new orchestration platform. Every action
below is an explicit call. No model or production socket is involved.

## Supported execution contract

- Linux `/proc`, POSIX `flock`, `setsid`, same local UID. Each root is a private,
  local node filesystem, not a synchronized/multi-host lease. Root ownership,
  no-symlink/hardlink checks and file-only pinned Git preparation extend P2a.
- **Only self-created immutable cooperating foreground synthetic fixtures.** The
  caller must not run arbitrary, daemonized, malicious same-UID or FD-closing
  workloads. This restriction is a precondition, not a security allowlist. No
  promise is made about sandboxing untrusted programs or hostile PID reuse races.
- `Popen(argv, shell=False, start_new_session=True, pass_fds=(leaseFd,))`, stdin
  `/dev/null`, cwd `work/<kind>/<id>/<attempt>/<generation>`. stdout/stderr are
  exclusive new local files under `logs/` with the same relative identity.
  No shell interpolation; only declared `${work}`/`${code.alias}` substitutions.
- Payload environment is exactly runtime `PATH=os.defpath`, fresh work `HOME`
  and `TMPDIR`, `LC_ALL=C.UTF-8`, explicit declared variables and `RUND_LEASE_FD`.
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
  run.json, result.json, lease.lock            local facts, terminal commit, fence
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
- `succeeded` only means a real `Popen.poll/wait` observed exit 0 and group/lease
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
  after the existing identity/group/lease convergence checks; its exitCode and
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
  confirm old group/lease convergence before allocating/spawning a new gen.
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

## Crash, identity, groups and FD facts

Before Popen the owner holds a stable per-run flock, publishes `starting`, and
passes its locked open file description to the payload. The foreground Python
fixture and every forked child **must keep `RUND_LEASE_FD` open until exit**.
The coordinator closes (never `LOCK_UN`s) its own FD when wait is observed or it
closes/crashes. A survivor still holds that same flock. The tests verify the
actual `/proc/<pid>/fd/<n>` inode/device and lock contention **after coordinator
closure**, including a forked child. This is not merely a result field.

- Crash after starting, before spawn: no second spawn is attempted. Task may
  become lost/null once lease is free. A Service without recorded identity
  remains cleanupUnknown, even after lease release: no proof of the old group
  can be reconstructed. Availability is intentionally sacrificed.
- Crash after spawn, before identity publication: `starting + busy lease`
  blocks release/cancel/restart; no process-name scan, blind kill or second
  spawn. The harness's separate ready/identity barriers are test-only and never
  read by runtime for recovery. They grant the harness cleanup authority only.
- Recorded survivors are checked against Linux boot ID, pid, `/proc/stat`
  starttime, pgid, session and UID. A new local lock owner may observe them and
  accept an explicit identity-checked stop. It has **no inherited wait result**.
- Runtime enumerates group facts read-only; every scan must succeed. Strong
  matching *live leader* is necessary at TERM and again at KILL escalation.
  Identity mismatch/unavailable → cleanupUnknown, no kill. No kill authority is
  inferred from pgid alone. The checks are conservative cooperating-process
  checks, not an atomic kernel guarantee against malicious races between the
  identity read and killpg.
- Observation can sample a matching zombie just after this owner's real
  `Popen.poll()` returned None; the owner's own lease FD still makes the lock
  busy in that interval. With a known matching boot, exact local Popen PID and
  strong matching non-live leader (and unambiguous group), the kernel makes at
  most one additional **real nonblocking poll**, then samples lease, leader and
  group again. Only an actual wait result releases the owner's FD and supplies
  an exit status. A second None, unreadable/mismatched identity, absent leader,
  unknown boot, reopened owner without Popen, live descendant or busy lease
  cannot turn this retry into success. No general cleanupUnknown retry, sleep,
  deadline extension, new persisted state or lifecycle/CLI policy is added.
- Stop sends group TERM, waits bounded time, then group KILL only if leader is
  still strongly matching. It checks actual target/group/lease convergence,
  not signal syscall success. If leader is gone but a child/lease remains,
  **no blind KILL of that group**, cleanupUnknown; new Service gen is blocked.
  The owner must explicitly resolve it. Dead zombies are recorded but counted
  as exited (not running); all non-zombie members must be gone and lease free.
  Harness subreaping is separate and not a runtime process manager.
- Once recorded original identity/group have disappeared and the lease is free:
  Task without a real wait is lost/null; Service can apply its declared policy
  to a new generation. Wait observed then coordinator crash before final
  publication can also become lost. No exit status is reconstructed from
  elapsed time, signals, report content or test fixture exit markers.

Stop facts retain before/after identities, group members, lease state, signals,
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
not candidate stop or process-name matching. Failures snapshot identity/lease/
stop evidence before cleanup. Each test asserts all recorded descendants exited
and reaped; preflight failure is a failure, not a skip. Crash windows use actual
SIGKILL and explicit starting/spawned/recorded/waited barriers; polls only wait
for these bounded barriers. Backoff uses a deterministic test clock.

Neither implementation nor tests claim macOS, cross-machine takeover, arbitrary
workloads, credential/remote Git/package execution, power-loss storage semantics,
agent/provider success, production integration or independent review PASS.
