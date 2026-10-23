"""Local workspace contract for independent source checkouts.

Derived from agent-platform/platform_paths.py; provenance is in SOURCE.json.
No sibling imports, platform marker or checkout-name assumptions are required.
"""
import os
from pathlib import Path
import sys


def workspace_root(source):
    """Explicit nonempty override wins; otherwise use real source repo's parent.

    Overrides expand '~' and resolve relative to the invocation cwd, matching
    the historical explicit-root interface. Resolve source symlinks, not their
    lexical ancestors. A pinned release must inject its actual workspace.
    """
    override = os.environ.get("PLATFORM_WORKSPACE")
    if override:
        return str(Path(override).expanduser().resolve())
    return str(Path(source).resolve().parent.parent)


if __name__ == "__main__":
    if len(sys.argv) != 2:
        raise SystemExit("usage: platform_paths.py <component entry path>")
    print(workspace_root(sys.argv[1]))
