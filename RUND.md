# Local runtime and service registry

`rund.py --config /absolute/node.json owner` is the foreground coordinator.
`start/stop service-sync` bootstraps only that coordinator from pinned Git objects;
`start/stop NAME`, `sync`, and `status [--machine]` use the same file protocol.
The caller exposes these through its service launcher (for example Make targets).

Node config `rund-node/v1` declares node, root, protocol, workspace, python, home,
path and named Git sources. Optional services maps names to argv/source aliases,
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

Retained tests: set a new absolute RUND_NATIVE_OUTPUT for runtime.test_native;
set RUND_CLI_TEST_OUTPUT for test_service_cli.py. No production workload or model
is used, and fixture directories are retained.
