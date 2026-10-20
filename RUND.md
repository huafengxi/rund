# Local runtime and service registry

`rund.py --config /absolute/node.json owner` is the foreground coordinator.
`start/stop service-sync` bootstraps only that coordinator from pinned Git objects;
`start/stop NAME`, `sync`, and `status [--machine]` use the same file protocol.
The caller exposes these through its service launcher (for example Make targets).

Node config `rund-node/v1` declares node, root, protocol, workspace, python, home,
path and named Git sources. For legacy installed snapshots, optional services maps names to argv/source aliases,
env/context; external lists services managed by their official API; interval is
registry reconciliation seconds. No hostnames or deployment defaults live here.
The runtime source alias is `runtime`. Bootstrap service defaults to service-sync.
Profiles and daemon declarations come from the caller workspace services tree.
Changing a managed declaration requires a higher profile version.

Services execute in foreground, in one same-UID/session/group contract; a
self-daemonizing profile requires an explicit foreground binding. External
third-party daemons retain their official lifecycle API. Bootstrap is deliberately
outside its own workload graph. Stop managed services before replacing an owner;
restarting a coordinator does not fabricate parent wait for surviving workloads.
Unknown/pending facts retain their original request identities and block replay.

Optional execution context declares absolute cwd/home, PATH and inherited names.
Credential references declare local file, optional loader argv and required names;
values are loaded in memory before spawn, never copied to declarations/results.
Callers own their PATH, source pins, registry values and source mirrors. Linux and
Darwin use native boot/start/UID/session/group facts and actual child wait.

## Shared desired state and minimal bootstrap

Set `registryFile` to an absolute locally transported snapshot, `bootstrapVersion`
to the fixed coordinator deployment version, and `seedServices` to an array of
service records needed to establish transport. In this mode node `services` and
`external` bindings are forbidden. Node `sources` need only contain the runtime
bootstrap pin and any sources needed to prepare the seed BEFORE the link exists.
`seedServices` is not an install list of business applications.

A record has `name`, `declaration` (a complete `rund/v1alpha1` Service), and optional
`status`, `external` and `stop_cmd` using existing probe/official-manager contracts.
The declaration contains its target node, executable version, fixed source pins
and dependency graph, argv, context, environment, credential references, desired
state and restart policy. No execution binding is looked up in node.json.

The activation unit is one JSON object:

```
{"apiVersion":"rund-registry/v1","sha256":"<canonical payload SHA-256>",
 "payload":{"node":"<caller node>","generation":1,"services":[<records>]}}
```

Canonical encoding is `runtime.prepare.canonical`. A checksum detects incomplete
or corrupt transport, not an adversarial publisher; transport permissions and
controller authorization belong to the caller. The controller is the sole writer
of registryFile's authority. The worker NEVER writes it. Its Owner atomically
persists a last-good snapshot and input digest in PRIVATE `registry-accepted.json`.
An invalid, absent or regressed input is diagnosed and leaves last-good workloads
and seed transport available. An equal generation must have identical input bytes
under canonical encoding. Node selection and all records validate before acceptance.

A higher generation replaces the whole non-seed desired set. An omitted accepted
service becomes a retained offline tombstone and its real generation is stopped
through the existing exact control path. Missing input does NOT mean deletion.
An omitted seed remains installed; override it explicitly to update/stop it.
Changing executable content requires a higher service version. Online/offline
changes need a higher snapshot generation, not a fake executable version bump.
No repeated synchronization creates another running generation. Unknown/pending
operations retain their original request identity and block automatic replay.
The CLI refuses worker-side managed start/stop: publish at the controller. `sync`
in this mode is a read-only confirmation; the Owner consumes on its interval.
`status` includes registry diagnostics and blocked targets as well as actual rows.

## Fixed remote sources

Source `repository` may be canonical local `file:///`, absolute `ssh://` or
credential-free `https://`. Every source still requires a full lowercase 40-hex
commit revision. SSH sources additionally require an absolute `sshConfig` file
reference; access keys remain in caller-controlled private storage. Git does not
load ambient global/system config, source config or hooks. The narrowly scoped
fetch enables only the declared transport, disables recursive submodule fetch,
uses BatchMode/no SSH connection sharing and a bounded fetch timeout. HTTPS does
not accept URL userinfo, query strings or fragments.

The node imports a pin into its private object cache, then uses the SAME verified
object export, read-only tree and prepare manifest path as local sources. A cached
verified commit works during transport outages; a new commit cannot report success
without object verification. A preparation failure leaves the previous running
version unchanged and is diagnosed. Registry reconciliation retries only completed
pre-spawn prepare failures, with a new exact request ID. Pending, unknown and
possibly-effectful outcomes are never turned into retry permission.

Retained tests: set a new absolute RUND_NATIVE_OUTPUT for runtime.test_native;
set RUND_CLI_TEST_OUTPUT for test_service_cli.py. No production workload or model
is used, and fixture directories are retained.
