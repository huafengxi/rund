# Local file Owner protocol

The production `RegistryOwner` extends this file Owner; see
[RUND.md](../../RUND.md) for the integrated contract. This page documents the
restricted standalone fixture interface. Explicit `python -B -m
runtime.file_owner owner --root ABS --protocol ABS --node NAME` starts one
foreground process with one Runtime/Kernel/node flock, without installing
transport or deployment. The owner remains until explicitly terminated,
independently of clients.

## Files and admission

Protocol is a private mode-0700 same-UID local directory, disjoint from runtime
and source repositories (including their ancestors). `binding.json` fixes
`apiVersion=rund-file/v0alpha1`, node and absolute runtimeRoot. Runtime retains
its original node.json and node.lock (no alternative node/lock key). Only the
owner initializes the protocol after obtaining Runtime's lock. Clients never
obtain Runtime or its lock. No shared/synchronized roots or hostile UID peers.

`requests/<requestId>.json` is persistent communication. Clients write/fsync a
private `.tmp-*` and use Linux atomic rename-no-replace, then fsync directory.
Requests are immutable; resend identical bytes is allowed, different bytes fails.
Owner ignores non-`.json` files and never reads a partial temp. Files must be
regular, single-link, same UID and private; symlink components are refused.
Single request maximum 65536 bytes; at most 16 directory entries per poll.
Unknown fields, JSON duplicate keys/nonfinite numbers, unknown versions, invalid
IDs, non-local node and invalid declarations/pins fail before payload effects.

Envelope always has exactly `apiVersion,requestId,node,action` plus:

| action | additional required fields |
|---|---|
| accept / release / submit | declaration, declarationDigest |
| rerun | id, oldAttempt, declaration, declarationDigest |
| start / observe / status | kind, id, attemptId |
| cancel / stop / restart | kind, id, attemptId, expectedGeneration |

Task accept does not start; release is already externally authorized intent.
Task rerun is explicit, requires terminal old attempt and new declaration attempt
and request ID. Task start/observe and Service status use the original methods;
status itself does not observe/tick or allocate generations. Service submit uses
original manual/on-failure/always policies. Controls require positive integer
expectedGeneration (Task=1, Service actual generation), Service attempt=`service`.
No blocking wait or subscription. Declaration is inline strict `rund/v1alpha1`,
SHA256 of S canonical JSON bytes in declarationDigest; only file:// full commit
pins, empty prepare and original tool checks. Declaration request IDs beginning
`fo-`/`fi-` are reserved. No W/agent/report/prompt semantics.

## Exact results and interruption

Outer request key is `fo-` + first 48 hex of SHA256(original requestId UTF-8).
S `Runtime.request/receipt` stores `{operation:file-owner, envelope:...}` and its
canonical digest in original `state/requests`. `send` returns this same body
digest (not a different hash of envelope-only JSON); receipt readers verify it.
This is the only execution intent record, not another queue/journal. Inner Service/control requests use deterministic
`fi-` keys. Task retains its original declaration requestId. Namespace collisions
are rejected by original digest check, never treated as equivalent.

Same rid/body returns original done record/result (not current state), even after
later policy progress. Done means this operation returned, NOT that a released
Task or submitted Service finished successfully. Inspect original run facts for
workload completion. Different body conflicts. An accepted pending record,
malformed record or unknown effect is never replayed, even with a new rid. Known
pending blocks its whole `(kind,id)` including Service ticks; corrupt/unknown
records block all mutations/ticks. Pure observation of Task cannot spawn/stop.
Done publication must succeed before completion is returned. If publication
fails, owner exposes diagnostic/pending, never fills success from snapshot. On
reopen pending is retained, not repaired, retried or converted to done. There is
no GC, retention/eviction, compensation, fallback or autostart. Client timeout or
exit means nothing about outcome: query the ORIGINAL rid, do not invent a new
rid to retry unknown effects.

`receipt` helper reads original S record through the fixed protocol binding,
without Runtime/node lock. `rejections/<filename>` is a diagnostic projection
for syntactic/admission failures only, NOT a done effect receipt. Original exact
record takes precedence; conflicts never overwrite it. Invalid names/temp files
may have no individual rejection file; last-observed diagnostics identify them.

## Projection and discovery

`snapshot.json` is atomic JSON, labelled `last-observed`, with owner identity,
ownerEpoch, node, observedAt, original heads/runs and diagnostics/blocked targets.
It is NOT an online probe or completion receipt. ownerEpoch is not generation;
mtime is not liveness. Run facts retain actual attempt/generation, full process
identity/phase/observation/waitObserved/exitCode (including null), and immutable
results remain authoritative. Snapshot read is lock-free. Existing old snapshot
survives owner exit; callers must treat it as last observed only.

Discovery reads original objects/heads and verifies declaration digests, node,
IDs and run binding. It is not a second registration system. Unindexed runs or
unknown directories block mutations (no alternate ID/rid route); no synthesized
head/attempt/generation or recovered wait. Task terminal is absorbing. Owner
restart uses original orphan observation, preserving lost/null, never old Popen
wait. Service tick retains backoff/hold and conservative identity/group/real-wait
gates. No per-run lease files or payload FD protocol are used. Pending Service does not tick; this intentionally sacrifices policy
availability rather than replaying ambiguous effects. Corrupt disk facts require
manual diagnosis outside this experiment.

One sequence: bounded requests -> discover/validate -> original Task observe or
eligible Service tick -> atomic snapshot. All actual spawn/wait/stop/identity/
group operations remain in the sole Kernel. No thread/worker or second Runtime.
Synchronous prepare/stop can delay polling. No fairness/latency guarantee, no
power-loss exactly-once or hostile same-UID race protection is claimed.

## Client

`python -B -m runtime.file_owner send --protocol ABS --request ABS_JSON`
only publishes then exits, without waiting. `receipt --protocol ABS --rid ID`
returns exact original record or null; `snapshot --protocol ABS` reads projection.
Neither read starts/recovers a workload. Unknown status/observe/start/control
is rejected before Runtime's directory-creating convenience readers are called;
status never allocates an unknown run. All invocations must select the candidate
explicitly via PYTHONPATH for standalone fixtures; production uses `rund.py`.

## Single-owner authority graph

```
client OS process --atomic persistent request--> private protocol/requests
     | exits                                          |
     +--receipt/snapshot read (no lock)                v
                            foreground Owner (one sequence, one Runtime)
                                      |                 |
                                 Task methods      Service methods/tick
                                      +--------+--------+
                                               v
                                  original one Kernel/node.lock
                                Popen / wait / stop / group
                                               |
                                     cooperating foreground payload
```

No client process or protocol helper holds Runtime. Fixtures may fork solely to
exercise the group gate; they never create another Runtime.
Internal Runtime intermediate directories retain S's original umask semantics,
fenced by the private runtime root; only external protocol directories require
private mode on every protocol component. Atomic readers allow an opened old
inode to have nlink=0 after a concurrent rename, but never nlink>1. This is not
permission to submit hardlinked requests or incomplete temp files. Top-level
snapshot observedAt is projection time; actual process observation time is the
original run observation.observedAt (or recorded startedAt), not file mtime.

Run tests with `RUND_FILE_TEST_OUTPUT=ABS_NEW_DIR python -B -m unittest
runtime.file_owner.test_owner -v`. Artifacts/identities/errors are retained.
