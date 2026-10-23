"""Deletion wire contract shared by list generation and sync consumption."""
GC_DIR_NAME = "gc"
LIST_PREFIX = "delete-list."
GLOB_UNSAFE = frozenset("*?[\n")
BARE_FAMILY_ENTRIES = ("agents", "task", "bot", "topic", "queue", "run", "services", "tasks", "configs")
PROTECTED_SYSTEM_PATHS = ("topic/dispatcher", "queue/dispatcher", "agents/topic/dispatcher", "agents/queue/dispatcher")
FORCE_MARK = " #FORCED:"


def split_force(e):
    """Split an entry into (path, audit-or-None) at the FORCE_MARK
    marker. Entries without the marker return
    (e, None). Only ``add --force`` writes marked entries; read /
    compression / reap pass them through."""
    i = e.find(FORCE_MARK)
    if i < 0:
        return e, None
    return e[:i], e[i + len(FORCE_MARK):]


def validate_path(e, force=False):
    """Validate path shape and protected assets without filesystem access.

    The audit flag lifts protected-asset refusal; structural guards always apply.
    """
    if not e or e != e.strip():
        return "empty or unstripped entry"
    if e.startswith("/"):
        return "absolute paths are not allowed"
    if any(c in GLOB_UNSAFE for c in e):
        return "glob-unsafe characters are not allowed"
    raw = e.rstrip("/")
    is_dir = e.endswith("/")
    if not raw or raw == ".":
        return "refusing the tree root"
    parts = raw.split("/")
    if any(p in ("", "..") for p in parts):
        return "empty or '..' path elements are not allowed"
    if len(parts) == 1 and parts[0] in BARE_FAMILY_ENTRIES:
        return (f"{parts[0]}/ is a BARE FAMILY CONTAINER — it names no "
                "participant; a deletion must name a concrete participant "
                "directory (task/<id>/, bot/<name>/, topic/<id>/, queue/<name>/). "
                "Refused even with --force; un-refuse = edit "
                "BARE_FAMILY_ENTRIES in agents-sync/delete_contract.py")
    if len(parts) == 2 and parts[0] == 'agents' and parts[1] in BARE_FAMILY_ENTRIES:
        return 'refusing a bare agent family container'
    if not force:
        for prot in PROTECTED_SYSTEM_PATHS:
            pp = prot.strip("/").split("/")
            if parts[:len(pp)] == pp:
                return (f"{prot}/ is a PROTECTED system asset (dispatcher "
                        "position mailbox) — deletion refused by default; "
                        "audited bypass: add --force")
    top = parts[0]
    if top == GC_DIR_NAME:
        if len(parts) == 2 and parts[1].startswith(LIST_PREFIX) \
                and not is_dir:
            return None  # list compression entry: legal
        return f"the {GC_DIR_NAME}/ tree is managed by gc.py itself"
    return None


