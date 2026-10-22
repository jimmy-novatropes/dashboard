"""Pull every Git repo under ROOTS. Skips repos that would fail (uncommitted changes,
diverged branches, no upstream) and logs each step plus a summary. Run it from PyCharm (Run 'pull_all_repos')."""
import subprocess
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path

# ===== SETTINGS =====
ROOTS = [Path(__file__).resolve().parent.parent]  # "Openflow Repos" folder; add more paths if needed
MAX_DEPTH = 5           # how deep to look for repos
AUTO_STASH = False      # True = stash uncommitted changes, pull, re-apply. False = skip dirty repos
FETCH_ONLY = False      # True = just fetch (never touches working files)
WORKERS = 8             # repos processed at the same time
TIMEOUT_SECONDS = 120   # per git command
VERBOSE = True          # True = log every step for every repo; False = only one line per finished repo
MAX_LIST = 5            # max commits / changed files listed per repo
SKIP_DIRS = {"node_modules", ".venv", "venv", "vendor", "dist", "build", "__pycache__"}
# ====================

C = {"OK": "\033[92m", "SKIP": "\033[93m", "FAIL": "\033[91m", "STEP": "\033[96m", "DIM": "\033[90m", "END": "\033[0m"}
_print_lock = threading.Lock()


def log(msg, color="", repo=None):
    stamp = datetime.now().strftime("%H:%M:%S")
    prefix = f"[{repo.name}] " if repo else ""
    with _print_lock:
        print(f"{C['DIM']}{stamp}{C['END']} {color}{prefix}{msg}{C['END'] if color else ''}", flush=True)


def step(repo, msg):
    if VERBOSE:
        log(msg, C["DIM"], repo)


def find_repos(folder, depth=0):
    if depth > MAX_DEPTH:
        return
    if (folder / ".git").exists():
        yield folder  # repo found, don't go deeper
        return
    try:
        children = sorted(p for p in folder.iterdir() if p.is_dir())
    except OSError as e:
        log(f"Can't read {folder}: {e}", C["SKIP"])
        return
    for child in children:
        if child.name not in SKIP_DIRS and not child.name.startswith("."):
            yield from find_repos(child, depth + 1)


def git(repo, *args):
    try:
        r = subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True,
                           timeout=TIMEOUT_SECONDS, stdin=subprocess.DEVNULL)
        return r.returncode, (r.stdout + r.stderr).strip()
    except subprocess.TimeoutExpired:
        return 1, f"timed out after {TIMEOUT_SECONDS}s (auth prompt or slow remote?)"


def error_lines(text):
    lines = [l.strip() for l in text.splitlines() if l.strip()]
    hits = [l for l in lines if any(w in l.lower() for w in ("fatal", "error", "diverg", "not possible", "denied", "timed out"))]
    return hits or lines[-3:] or ["unknown error"]


def short_list(items):
    shown = items[:MAX_LIST]
    more = f" (+{len(items) - MAX_LIST} more)" if len(items) > MAX_LIST else ""
    return shown, more


def pull(repo):
    """Returns (status, summary, details) where status is OK / SKIP / FAIL."""
    step(repo, "checking branch...")
    _, branch = git(repo, "rev-parse", "--abbrev-ref", "HEAD")
    if branch == "HEAD":
        return "SKIP", "detached HEAD (not on a branch)", []

    code, upstream = git(repo, "rev-parse", "--abbrev-ref", "@{u}")
    if code != 0:
        return "SKIP", f"[{branch}] no upstream branch set", []
    step(repo, f"on '{branch}' tracking '{upstream}', fetching...")

    code, out = git(repo, "fetch", "--prune")
    if code != 0:
        return "FAIL", f"[{branch}] fetch failed", error_lines(out)

    _, counts = git(repo, "rev-list", "--left-right", "--count", "HEAD...@{u}")
    ahead, behind = (int(x) for x in counts.split())
    step(repo, f"{behind} commit(s) behind, {ahead} ahead of {upstream}")
    ahead_note = f", {ahead} local commit(s) not pushed" if ahead else ""

    if FETCH_ONLY:
        return "OK", f"[{branch}] fetched, {behind} behind{ahead_note}", []
    if behind == 0:
        return "OK", f"[{branch}] already up to date{ahead_note}", []

    _, dirty = git(repo, "status", "--porcelain")
    changed = [l[3:] for l in dirty.splitlines() if l.strip()]
    if changed and not AUTO_STASH:
        files, more = short_list(changed)
        return "SKIP", f"[{branch}] NOT pulled: {len(changed)} uncommitted file(s), {behind} commit(s) waiting", \
            [f"changed: {f}" for f in files] + ([more.strip()] if more else [])

    if ahead and behind:
        return "FAIL", f"[{branch}] NOT pulled: diverged ({ahead} local vs {behind} remote commits) - needs a manual merge/rebase", []

    _, before = git(repo, "rev-parse", "HEAD")
    step(repo, f"pulling {behind} commit(s)" + (" with autostash" if changed else "") + "...")
    code, out = git(repo, "pull", "--ff-only", *(["--autostash"] if AUTO_STASH else []))
    if code != 0:
        return "FAIL", f"[{branch}] pull failed", error_lines(out)

    _, new_commits = git(repo, "log", "--oneline", "--no-decorate", f"{before}..HEAD")
    commits = new_commits.splitlines()
    shown, more = short_list(commits)
    return "OK", f"[{branch}] PULLED {len(commits)} new commit(s)", [f"+ {c}" for c in shown] + ([more.strip()] if more else [])


def main():
    start = time.time()
    for root in ROOTS:
        log(f"Scanning {root}" + ("" if root.exists() else "  (folder not found, skipping)"), C["STEP"])
    repos = [r for root in ROOTS if root.exists() for r in find_repos(root)]
    log(f"Found {len(repos)} repo(s). Mode: {'fetch only' if FETCH_ONLY else 'pull --ff-only'}"
        f"{', autostash on' if AUTO_STASH else ''}. Workers: {WORKERS}\n", C["STEP"])

    results, done = [], 0
    with ThreadPoolExecutor(WORKERS) as pool:
        futures = {pool.submit(pull, r): r for r in repos}
        for fut in as_completed(futures):
            repo = futures[fut]
            try:
                status, summary, details = fut.result()
            except Exception as e:  # never let one repo stop the run
                status, summary, details = "FAIL", "script error", [repr(e)]
            done += 1
            results.append((repo, status, summary, details))
            log(f"({done}/{len(repos)}) {status:<4} {summary}", C[status], repo)
            for d in details:
                log(f"      {d}", C[status], repo)

    groups = [
        ("PULLED (new commits)", lambda s, m: s == "OK" and "PULLED" in m),
        ("UP TO DATE / FETCHED", lambda s, m: s == "OK" and "PULLED" not in m),
        ("NOT PULLED - skipped", lambda s, m: s == "SKIP"),
        ("NOT PULLED - failed (needs attention)", lambda s, m: s == "FAIL"),
    ]
    print(f"\n{'=' * 25} SUMMARY {'=' * 25}")
    for title, match in groups:
        rows = sorted((r for r in results if match(r[1], r[2])), key=lambda r: r[0].name.lower())
        if not rows:
            continue
        print(f"\n{title} ({len(rows)})")
        for repo, status, summary, details in rows:
            print(f"  {C[status]}{repo.name:<35}{C['END']} {summary}")
            if status == "FAIL":
                for d in details:
                    print(f"  {'':<35}   {d}")
                print(f"  {'':<35}   {C['DIM']}{repo}{C['END']}")

    n = lambda s: sum(1 for r in results if r[1] == s)
    print(f"\n{n('OK')} ok, {n('SKIP')} skipped, {n('FAIL')} failed, {len(results)} total "
          f"in {time.time() - start:.1f}s")


if __name__ == "__main__":
    main()
