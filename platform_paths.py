"""Local workspace-root contract v1 (source: agent-platform/platform_paths.py).

Source identity follows realpath, independently of workspace overrides.
No platform marker, integrated checkout, or module list is required.
"""
import os
from pathlib import Path
import sys

def workspace_root(source):
    """Nonempty PLATFORM_WORKSPACE (expanded/absolute), else real repo parent.

    Callers with CLI/module-specific roots choose those before calling here.
    serviced has no CLI root option; its override is PLATFORM_WORKSPACE.
    """
    override = os.environ.get("PLATFORM_WORKSPACE")
    if override:
        return str(Path(override).expanduser().resolve())
    return str(Path(source).resolve().parent.parent)


if __name__ == "__main__":
    if len(sys.argv) != 2:
        raise SystemExit("usage: platform_paths.py <component entry path>")
    print(workspace_root(sys.argv[1]))
