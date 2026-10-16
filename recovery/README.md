# Recovery provenance — task rd6-s-175748

This is a standalone **source candidate**, not a deployment. No production
profile, startup path, service-sync activation, scheduler policy or Task
supervision is changed. The production gate stays closed.

## Identity and projection

- `source-refs.json`: original serviced main, original archive/retired refs and
  symbolic-ref information, full per-ref object closures, and original target
  refs. `main` may advance by descendant commits; the named preserve refs must
  retain their exact original OIDs.
- `source-map.json`: source identity `agent-platform`, complete baseline/frozen
  SHA, all first-parent source commits in order, exact path/blob/mode/deletion
  mappings and target commits. Empty projections are explicit no-ops; no
  cross-module commit was cherry-picked or filtered into a replacement history.
- `adaptations.json`: separate adaptation commit and every changed path's
  projected/adapted mode/blob, explanation, plus shared root-README ownership.
- `net-paths.json`: every non-recovery original/source/projected/delivered path,
  including additions and deletions. A path with no source version is a local
  standalone adaptation, not an attributed source increment.
- `dependencies.json`: full agentd revision and envscrub interface/content pin.
  No agentd code or scrub policy list is copied into this repository.

Original main `6124dba2a36c1144afd963a3a42aa76a5cc053f7` remains the ancestor;
source baseline `990253da3b5c5ec83fd43a2a463f39ed6d42aadf` through frozen
`4ac4631fbdbf27f3c400805b233dc1d7a9892d46` contains 13 first-parent records:
3 projections, 10 no-ops. Source root README mechanism material is mapped into
the standalone README; portable/installers, other modules, integrated deployment
and integration-runner material belong elsewhere. No installer is revived.

## Reproduce without production access

Prepare independent clones from caller-chosen origins. Preserve the source
history (the frozen SHA must be available); do not use a production checkout as
a harness. Check out agentd at
`9625bdc15f853e49a6312d20dc429e23330e3d89`. Supply an existing real Bash3.2.
No test downloads/builds it. Example, replacing all placeholders:

```sh
AGENTD_DIR=/isolated/pinned-agentd BASH32=/isolated/tools/bash-3.2 \
  /path/to/conda/python -B /isolated/serviced/recovery/run-tests.py \
  --output /isolated/new-test-output

/path/to/conda/python -B /isolated/serviced/recovery/verify.py \
  --source /isolated/source.git --origin /path/to/candidate-origin.git \
  --output /isolated/new-verification.json
```

`verify.py` is read-only against repositories: independently reconstructs each
owned-path source delta and target tree, checks source-order/messages, original
refs/closure/ancestor, adaptation/net-path declarations and fsck. `--origin`
checks actual named preserve refs rather than only local remote-tracking refs.
Review may additionally regenerate source closures from historical originals;
metadata does not purport to replace that independent oracle.

`run-tests.py` enumerates all seven applicable test scripts, verifies dependency
checkout HEAD/module digest and actual Bash3.2, then gives each script an isolated
HOME/TMPDIR. Full logs are retained under the new output root. All fixtures are
self-created, with no real services or model calls. `test-results.json` records
the successful implementation run; machine-specific raw logs and paths remain
in task evidence, not this public repository.

The initial full-shell fixture omitted its synthetic override directory and
failed; it was corrected, then all seven scripts passed in a fresh run. This
was a fixture error, not a service lifecycle failure or a skipped criterion.
Actual Linux Bash3.2 (3.2.0(2)-release) ran 25 complete-loop cases with a synthetic
Python sync observer and checked each observed shell version. No macOS or
production/multi-host integration is claimed. Source candidates still await
fixed-revision integration and the separately registered independent review.
