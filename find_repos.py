"""List every Git repo under ROOTS (e.g. the "Openflow Repos" folder).
Prints a table plus a ready-to-paste Python list. Other scripts can `from find_repos import find_all`."""
import subprocess
from pathlib import Path

# ===== SETTINGS =====
ROOTS = [Path(__file__).resolve().parent.parent]  # "Openflow Repos" folder; add more paths if needed
MAX_DEPTH = 5           # how deep to look for repos
ONLY_GITHUB = False     # True = only repos whose 'origin' remote is on github.com
# Folders to leave out, as paths relative to the ROOTS folder (case-insensitive).
# A parent folder excludes every repo under it. "jackie_london" does NOT match "jackie_london_3".
EXCLUDE = {
    "jackie_london",
    "jackie_london_2",
}
SKIP_DIRS = {"node_modules", ".venv", "venv", "vendor", "dist", "build", "__pycache__"}
# ====================


def _walk(folder, depth=0):
    if depth > MAX_DEPTH:
        return
    if (folder / ".git").exists():
        yield folder  # repo found, don't go deeper
        return
    try:
        children = sorted((p for p in folder.iterdir() if p.is_dir()), key=lambda p: p.name.lower())
    except OSError:
        return
    for child in children:
        if child.name not in SKIP_DIRS and not child.name.startswith("."):
            yield from _walk(child, depth + 1)


def is_excluded(repo, root):
    try:
        rel = [p.lower() for p in repo.relative_to(root).parts]
    except ValueError:
        return False
    for e in EXCLUDE:
        ep = [p.lower() for p in Path(e).parts]
        if ep and rel[:len(ep)] == ep:
            return True
    return False


def remote_url(repo):
    try:
        r = subprocess.run(["git", "-C", str(repo), "remote", "get-url", "origin"],
                           capture_output=True, text=True, timeout=10, stdin=subprocess.DEVNULL)
        return r.stdout.strip() if r.returncode == 0 else ""
    except (OSError, subprocess.TimeoutExpired):
        return ""


def find_all(roots=None, only_github=None):
    """Return a sorted list of (path, remote_url) for every repo found."""
    roots = roots or ROOTS
    only_github = ONLY_GITHUB if only_github is None else only_github
    found = {}
    for root in roots:
        root = Path(root).expanduser().resolve()
        for repo in _walk(root):
            if is_excluded(repo, root):
                continue
            url = remote_url(repo)
            if only_github and "github.com" not in url.lower():
                continue
            found[repo] = url
    return sorted(found.items(), key=lambda kv: str(kv[0]).lower())


def find_excluded(roots=None):
    """Repos under ROOTS that EXCLUDE leaves out."""
    out = []
    for root in roots or ROOTS:
        root = Path(root).expanduser().resolve()
        out += [r for r in _walk(root) if is_excluded(r, root)]
    return sorted(set(out), key=lambda p: str(p).lower())


def main():
    repos = find_all()
    if not repos:
        print("No repos found under:", *ROOTS, sep="\n  ")
        return
    width = max(len(str(p)) for p, _ in repos)
    print(f"Found {len(repos)} repo(s):\n")
    for path, url in repos:
        print(f"  {str(path):<{width}}  {url or '(no origin remote)'}")
    print("\nREPOS = [")
    for path, _ in repos:
        print(f'    r"{path}",')
    print("]")
    skipped = find_excluded()
    if skipped:
        print(f"\nExcluded ({len(skipped)}):")
        for p in skipped:
            print(f"  {p}")


if __name__ == "__main__":
    main()
