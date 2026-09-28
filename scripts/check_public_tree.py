"""Conservative public-tree guard; additional manual content review is required."""
from pathlib import Path
import re
import subprocess
import sys

root = Path(__file__).resolve().parents[1]
names = subprocess.check_output(["git", "ls-files", "-z"], cwd=root).decode().split("\0")
patterns = [re.compile(x) for x in [
    r"/(?:Users|home)/[A-Za-z0-9_.-]+/", r"/(?:lustre|dssg)/",
    r"-----BEGIN [A-Z ]*PRIVATE KEY-----", r"\bgh[pousr]_[A-Za-z0-9]{20,}",
    r"\bsk-[A-Za-z0-9_-]{20,}",
]]
bad = []
for name in filter(None, names):
    path = root / name
    if path.is_symlink():
        bad.append((name, "symlink"))
        continue
    if (path.suffix.lower() in {".pdf", ".sqlite", ".db", ".pem", ".key", ".zip"}
            or path.name.startswith(".env") or "credentials" in path.parts):
        bad.append((name, "private file class"))
        continue
    data = path.read_bytes()
    if len(data) > 1_000_000 or b"\0" in data:
        bad.append((name, "binary or oversized file"))
        continue
    text = data.decode("utf-8")
    if any(p.search(text) for p in patterns):
        bad.append((name, "sensitive pattern"))
for name, reason in bad:
    print(f"{name}: {reason}")
print(f"Public tree: {len(list(filter(None, names)))} tracked files; {len(bad)} findings")
sys.exit(bool(bad))
