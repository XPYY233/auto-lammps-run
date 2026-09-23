"""Metadata discovery CLI. Private metadata must never be written in a Git tree."""
import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path

from .zotero import LocalZotero, ZoteroError


def save_private(path: Path, payload: dict) -> None:
    path = path.expanduser().resolve()
    if any((parent / ".git").exists() for parent in [path.parent, *path.parent.parents]):
        raise ValueError("Output must be outside every Git working tree")
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    # Exclusive creation preserves earlier audits and refuses symlink overwrite.
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=2)
        handle.write("\n")


def main() -> None:
    parser = argparse.ArgumentParser(description="Read-only local Zotero discovery")
    parser.add_argument("--query", default="LAMMPS")
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    try:
        result = LocalZotero().discover(args.query)
        result["retrieved_at"] = datetime.now(timezone.utc).isoformat()
        save_private(args.output, result)
    except (ZoteroError, ValueError, OSError) as exc:
        parser.exit(1, f"Discovery failed: {type(exc).__name__}. Check local API and private output location.\n")
    print(json.dumps({key: result[key] for key in ("search_hits", "unique_papers", "consistency")}))


if __name__ == "__main__":
    main()
