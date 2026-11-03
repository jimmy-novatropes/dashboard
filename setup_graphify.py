"""
setup_graphify.py - install/update Graphify and wire it into Claude (Windows).

What it does (safe to re-run any time):
  1. Installs or upgrades Graphify (MCP server + SQL parser) via uv, falling back to pip.
  2. Installs the global /graphify skill for Claude Code.
  3. For each repo (REPOS + everything find_repos.py finds, minus its EXCLUDE list):
       - hides Graphify's files from git locally (.git/info/exclude - nothing to commit)
       - builds the graph (first run) or updates it (later runs), code-only = no API key
       - generates GRAPH_REPORT.md + graph.html
       - writes CLAUDE.md section + PreToolUse hook (.claude/settings.json)
       - installs git hooks so the graph rebuilds on commit/checkout/merge
  4. Merges all repo graphs into ONE local file and registers ONE MCP server ("graphify")
     in the Claude desktop app (chat + Cowork) and Claude Code. Old per-repo
     "graphify-..." servers are removed. Config is backed up first.
  5. Removes Graphify's hooks from excluded repos.
  6. Lists repos that still show Graphify changes in git (tracked CLAUDE.md etc).

Nothing is pushed to GitHub. Run:  python setup_graphify.py
"""
from pathlib import Path

# ============================ SETTINGS ============================
# Extra repos to always include (full paths). Usually empty: AUTO_FIND_REPOS finds them.
REPOS = [
    r"C:\Users\jagom\Documents\Openflow Repos\dashboard",
]
AUTO_FIND_REPOS = True           # also add every repo find_repos.py finds (its ROOTS / EXCLUDE / ONLY_GITHUB)

CODE_ONLY = True                 # True = local AST only, no API key, no data leaves the machine
LLM_BACKEND = ""                 # used only if CODE_ONLY=False: "claude", "openai", "gemini", "ollama", ...
GRAPHIFY_PACKAGE = "graphifyy[mcp,sql]"
STRICT_HOOK = False              # True = Claude Code must run `graphify query` before its first raw file read
INSTALL_GIT_HOOKS = True         # auto-rebuild graph on commit / checkout / merge
SETUP_CLAUDE_CODE_PROJECT = True # CLAUDE.md section + PreToolUse hook in each repo
GIT_LOCAL_EXCLUDE = True         # add Graphify's files to .git/info/exclude (local only, never committed)
GIT_EXCLUDE_ENTRIES = ["graphify-out/", ".claude/", ".gitattributes", "CLAUDE.md", ".graphifyignore"]  # only hides UNTRACKED files
# Files Graphify skips in every repo (written to a local .graphifyignore, gitignore syntax)
GRAPHIFY_IGNORE = ["archive/", "*_old*.py", "*_backup*.py", "*_old*.js", "*_backup*.js"]
CLEAN_EXCLUDED_REPOS = True      # remove Graphify git hooks + Claude hook from repos in find_repos EXCLUDE

MERGE_GRAPHS = True              # True = one merged graph + one MCP server; False = one server per repo
MERGED_GRAPH = Path(__file__).resolve().parent.parent / "_graphify" / "merged-graph.json"  # outside every repo
MCP_SERVER_NAME = "graphify"     # name used when MERGE_GRAPHS = True
MCP_NAME_PREFIX = "graphify-"    # per-repo names when MERGE_GRAPHS = False (old ones with this prefix get removed)

SETUP_CLAUDE_DESKTOP_MCP = True  # Claude desktop app (chat + Cowork)
EXTRA_DESKTOP_CONFIGS = []       # paste the path "Edit config" opens if the app uses another file
SETUP_CLAUDE_CODE_MCP = True     # Claude Code (only if the `claude` CLI is installed)
FORCE_REBUILD = False            # True = full re-scan (use after big refactors/deletions)

GLOBAL_CLAUDE_MD = Path.home() / ".claude" / "CLAUDE.md"   # usage rules for every Claude Code session
WRITE_GLOBAL_USAGE_RULES = True
NIGHTLY_REFRESH = True           # Windows scheduled task that re-runs this script every night
NIGHTLY_TIME = "03:15"           # after the tower's 02:xx sweeps
NIGHTLY_TASK_NAME = "Graphify nightly refresh"
LOG_FILE = MERGED_GRAPH.parent / "last_run.log"            # full output of the latest run
# ==================================================================

import json
import os
import shutil
import subprocess
import sys
import time


class _Tee:
    """Print to the console (if any) and to LOG_FILE - pythonw / scheduled runs have no console."""
    def __init__(self, path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.f = open(path, "w", encoding="utf-8")
        self.console = sys.__stdout__

    def write(self, text):
        self.f.write(text)
        if self.console:
            try:
                self.console.write(text)
            except (OSError, ValueError):
                pass

    def flush(self):
        self.f.flush()
        if self.console:
            try:
                self.console.flush()
            except (OSError, ValueError):
                pass


NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)   # no console flashes during the nightly run


def log(msg):
    print(f"[setup] {time.strftime('%H:%M:%S')} {msg}", flush=True)


def run(cmd, cwd=None, check=True, env=None, quiet=False):
    if not quiet:
        print("   $ " + " ".join(str(c) for c in cmd), flush=True)
    r = subprocess.run([str(c) for c in cmd], cwd=cwd, env=env, text=True, stdin=subprocess.DEVNULL,
                       capture_output=True, encoding="utf-8", errors="replace", creationflags=NO_WINDOW)
    out = (r.stdout or "") + (r.stderr or "")
    tail = "\n".join(out.strip().splitlines()[-4:])
    if tail and not quiet:
        print("     " + tail.replace("\n", "\n     "), flush=True)
    if check and r.returncode != 0:
        err = RuntimeError(f"Command failed ({r.returncode}): {' '.join(map(str, cmd))}")
        err.output = out
        raise err
    return r


def find_exe(name):
    """Find an executable on PATH or in the usual uv / pip --user script dirs."""
    p = shutil.which(name)
    if p:
        return p
    candidates = [
        Path.home() / ".local" / "bin",
        Path(os.environ.get("APPDATA", "")) / "Python" / f"Python{sys.version_info.major}{sys.version_info.minor}" / "Scripts",
        Path(sys.executable).parent / "Scripts",
        Path(sys.executable).parent,
    ]
    for d in candidates:
        for ext in ("", ".exe", ".cmd"):
            f = d / (name + ext)
            if f.exists():
                return str(f)
    return None


# ---------- 1. install / upgrade ----------
def install_graphify():
    if sys.version_info < (3, 10):
        sys.exit("Python 3.10+ required.")
    uv = find_exe("uv")
    if not uv:
        log("uv not found - installing it with pip")
        run([sys.executable, "-m", "pip", "install", "--user", "-U", "uv"])
        uv = find_exe("uv")
    if uv:
        log("Installing/upgrading Graphify with uv")
        run([uv, "tool", "install", "--upgrade", GRAPHIFY_PACKAGE])
        run([uv, "tool", "update-shell"], check=False, quiet=True)
    else:
        log("Falling back to pip --user")
        run([sys.executable, "-m", "pip", "install", "--user", "-U", GRAPHIFY_PACKAGE])

    gfy, mcp = find_exe("graphify"), find_exe("graphify-mcp")
    if not gfy or not mcp:
        sys.exit("Installed, but graphify / graphify-mcp not found. Open a new terminal and re-run.")
    run([gfy, "--version"], check=False)
    return gfy, mcp


# ---------- 3. per-repo ----------
def git_local_exclude(repo: Path):
    info = repo / ".git" / "info"
    if not (repo / ".git").is_dir():
        return
    info.mkdir(parents=True, exist_ok=True)
    f = info / "exclude"
    lines = f.read_text(encoding="utf-8", errors="replace").splitlines() if f.exists() else []
    missing = [e for e in GIT_EXCLUDE_ENTRIES if e not in lines]
    if missing:
        with f.open("a", encoding="utf-8") as fh:
            if lines and lines[-1].strip():
                fh.write("\n")
            fh.write("# graphify (local only)\n" + "\n".join(missing) + "\n")


IGNORE_START, IGNORE_END = "# graphify-managed:start", "# graphify-managed:end"


def write_graphifyignore(repo: Path):
    """Keep a managed block in the repo's .graphifyignore (user lines outside the block are kept)."""
    f = repo / ".graphifyignore"
    text = f.read_text(encoding="utf-8", errors="replace") if f.exists() else ""
    block = IGNORE_START + "\n" + "\n".join(GRAPHIFY_IGNORE) + "\n" + IGNORE_END + "\n"
    if IGNORE_START in text and IGNORE_END in text:
        before, rest = text.split(IGNORE_START, 1)
        after = rest.split(IGNORE_END, 1)[1].lstrip("\r\n")
        new = before + block + after
    else:
        new = (text.rstrip("\n") + "\n" if text.strip() else "") + block
    if new != text:
        f.write_text(new, encoding="utf-8")


class EmptyRepo(Exception):
    pass


def build_repo(gfy, repo: Path):
    graph = repo / "graphify-out" / "graph.json"
    env = os.environ.copy()
    if CODE_ONLY:
        env["GRAPHIFY_BACKEND"] = "none"

    if GIT_LOCAL_EXCLUDE:
        git_local_exclude(repo)
    if GRAPHIFY_IGNORE:
        write_graphifyignore(repo)

    try:
        if graph.exists() and not FORCE_REBUILD:
            log(f"Updating graph: {repo}")
            run([gfy, "update", repo], cwd=repo, env=env)
        else:
            log(f"Building graph: {repo}")
            cmd = [gfy, "extract", repo]
            if CODE_ONLY:
                cmd.append("--code-only")
            elif LLM_BACKEND:
                cmd += ["--backend", LLM_BACKEND]
            if FORCE_REBUILD:
                cmd.append("--force")
            run(cmd, cwd=repo, env=env)
    except RuntimeError as e:
        if "graph is empty" in getattr(e, "output", ""):
            raise EmptyRepo("no code files")
        raise

    cmd = [gfy, "cluster-only", repo]
    if CODE_ONLY:
        cmd.append("--no-label")
    run(cmd, cwd=repo, env=env, check=False)

    if SETUP_CLAUDE_CODE_PROJECT:
        run([gfy, "claude", "install"] + (["--strict"] if STRICT_HOOK else []), cwd=repo, check=False)

    if INSTALL_GIT_HOOKS and (repo / ".git").exists():
        run([gfy, "hook", "install"], cwd=repo, check=False)

    if not graph.exists():
        raise RuntimeError(f"No graph.json produced for {repo}")
    return graph


def clean_excluded(gfy, repos):
    for repo in repos:
        hook = repo / ".git" / "hooks" / "post-merge"
        settings = repo / ".claude" / "settings.json"
        has_hook = hook.exists() and "graphify" in hook.read_text(encoding="utf-8", errors="replace")
        has_settings = settings.exists() and "graphify" in settings.read_text(encoding="utf-8", errors="replace")
        if not (has_hook or has_settings):
            continue
        log(f"Removing Graphify hooks from excluded repo: {repo}")
        run([gfy, "hook", "uninstall"], cwd=repo, check=False)
        run([gfy, "claude", "uninstall"], cwd=repo, check=False)
        if GIT_LOCAL_EXCLUDE:
            git_local_exclude(repo)  # leftover graphify-out/ stays out of git status; delete it whenever


def graphify_git_changes(repo: Path):
    r = run(["git", "-C", repo, "status", "--porcelain", "--", *[e.rstrip("/") for e in GIT_EXCLUDE_ENTRIES]],
            check=False, quiet=True)
    return [l[3:] for l in r.stdout.splitlines() if l.strip()]


# ---------- 4. MCP config ----------
def desktop_config_paths():
    paths = []
    appdata = os.environ.get("APPDATA")
    if appdata:
        paths.append(Path(appdata) / "Claude" / "claude_desktop_config.json")
    local = os.environ.get("LOCALAPPDATA")
    if local:  # Microsoft Store / MSIX install
        for pkg in (Path(local) / "Packages").glob("*Claude*"):
            cfg_dir = pkg / "LocalCache" / "Roaming" / "Claude"
            if cfg_dir.is_dir():
                paths.append(cfg_dir / "claude_desktop_config.json")
        paths += list((Path(local) / "Claude-Data").glob("*/claude_desktop_config.json"))
    paths += [Path(p) for p in EXTRA_DESKTOP_CONFIGS]
    existing = [p for p in paths if p.exists()]
    return existing or paths[:1]


def is_ours(name):
    return name == MCP_SERVER_NAME or name.startswith(MCP_NAME_PREFIX)


def update_desktop_config(servers: dict):
    changed = False
    for cfg in desktop_config_paths():
        cfg.parent.mkdir(parents=True, exist_ok=True)
        data = {}
        if cfg.exists():
            data = json.loads(cfg.read_text(encoding="utf-8") or "{}")
        current = {k: v for k, v in data.get("mcpServers", {}).items() if is_ours(k)}
        if current == servers:
            log(f"Claude desktop config already up to date ({cfg})")
            continue
        if cfg.exists():
            backup = cfg.with_name(f"claude_desktop_config.backup-{time.strftime('%Y%m%d-%H%M%S')}.json")
            shutil.copy2(cfg, backup)
            log(f"Backed up config -> {backup.name}")
        mcp = data.setdefault("mcpServers", {})
        removed = [k for k in list(mcp) if is_ours(k)]
        for k in removed:
            del mcp[k]
        mcp.update(servers)
        cfg.write_text(json.dumps(data, indent=2), encoding="utf-8")
        changed = True
        log(f"Claude desktop config: removed {len(removed)} old graphify server(s), added {len(servers)}  ({cfg})")
    return changed


def update_claude_code_mcp(servers: dict):
    claude = find_exe("claude")
    if not claude:
        log("`claude` CLI not found - skipping Claude Code MCP registration")
        return
    old, current = [], {}
    cc = Path.home() / ".claude.json"
    if cc.exists():
        try:
            registered = json.loads(cc.read_text(encoding="utf-8")).get("mcpServers", {})
            old = [k for k in registered if is_ours(k)]
            current = {k: {"command": registered[k].get("command"), "args": registered[k].get("args", [])} for k in old}
        except (ValueError, OSError, AttributeError):
            pass
    if current == servers:
        log("Claude Code MCP already up to date")
        return
    for name in set(old) | set(servers):
        run([claude, "mcp", "remove", name, "-s", "user"], check=False, quiet=True)
    for name, s in servers.items():
        run([claude, "mcp", "add", "--transport", "stdio", "-s", "user", name, "--",
             s["command"], *s["args"]], check=False)
    log(f"Claude Code: removed {len(old)} old graphify server(s), added {len(servers)}")


USAGE_START, USAGE_END = "<!-- graphify-usage:start -->", "<!-- graphify-usage:end -->"
USAGE_RULES = f"""{USAGE_START}
## Graphify MCP (all client repos)
- MCP server `graphify` = one merged graph of every client repo (`{MERGED_GRAPH}`, refreshed nightly).
  Use it for structure: who calls what, impact of a change, the same pattern across clients.
- Client known -> always pass `project_path` = that repo folder (e.g. `{MERGED_GRAPH.parent.parent / 'stellaromics'}`).
  Without it the merged graph mixes clients and returns unrelated files.
- Keep answers small: `token_budget` 500-1000, `depth` 1-2, `context_filter=["call"]` when tracing calls.
- Never `get_node` / `get_neighbors` a generic name (`lambda_handler`, `handler`, `main`, `load_secrets`): use the
  full node id (`<repo>::<path>_<name>`) or a client-specific function name.
- `god_nodes` on the merged graph is copied boilerplate: use it per repo with `exclude_hubs_percentile=99`.
{USAGE_END}
"""


def write_usage_rules(path: Path):
    path.parent.mkdir(parents=True, exist_ok=True)
    text = path.read_text(encoding="utf-8", errors="replace") if path.exists() else ""
    if USAGE_START in text and USAGE_END in text:
        before, rest = text.split(USAGE_START, 1)
        new = before + USAGE_RULES.rstrip("\n") + rest.split(USAGE_END, 1)[1]
    else:
        new = (text.rstrip("\n") + "\n\n" if text.strip() else "") + USAGE_RULES
    if new != text:
        path.write_text(new, encoding="utf-8")
        log(f"Usage rules written to {path}")


def register_nightly_task():
    if os.name != "nt":
        return
    py = Path(sys.executable)
    pyw = py.with_name("pythonw.exe")
    exe = pyw if pyw.exists() else py
    action = f'"{exe}" "{Path(__file__).resolve()}"'
    q = run(["schtasks", "/Query", "/TN", NIGHTLY_TASK_NAME, "/XML"], check=False, quiet=True)
    if q.returncode == 0 and str(Path(__file__).resolve()) in q.stdout and f"T{NIGHTLY_TIME}:00" in q.stdout:
        log(f"Nightly task already scheduled ({NIGHTLY_TIME})")
        return
    r = run(["schtasks", "/Create", "/F", "/SC", "DAILY", "/ST", NIGHTLY_TIME, "/TN", NIGHTLY_TASK_NAME,
             "/TR", action], check=False, quiet=True)
    if r.returncode == 0:
        log(f"Nightly task scheduled: '{NIGHTLY_TASK_NAME}' at {NIGHTLY_TIME} (log: {LOG_FILE})")
    else:
        log(f"Could not schedule nightly task: {(r.stdout + r.stderr).strip()}")


def per_repo_name(repo: Path, root: Path):
    try:
        rel = "-".join(repo.relative_to(root).parts)
    except ValueError:
        rel = repo.name
    return MCP_NAME_PREFIX + "".join(c if c.isalnum() or c in "-_" else "-" for c in rel.lower())


def main():
    sys.stdout = sys.stderr = _Tee(LOG_FILE)
    log(f"Run started {time.strftime('%Y-%m-%d')}")
    here = Path(__file__).resolve().parent
    sys.path.insert(0, str(here))
    import find_repos

    gfy, mcp = install_graphify()

    excluded = find_repos.find_excluded() if AUTO_FIND_REPOS else []
    if CLEAN_EXCLUDED_REPOS and excluded:
        clean_excluded(gfy, excluded)  # before the skill install: `claude uninstall` also removes the global skill

    log("Installing global /graphify skill for Claude Code")
    run([gfy, "install", "--platform", "claude"], check=False)

    excluded_set = {str(p) for p in excluded}
    repo_list = [str(Path(r).expanduser().resolve()) for r in REPOS]
    if AUTO_FIND_REPOS:
        repo_list += [str(p) for p, _ in find_repos.find_all()]
    repo_list = [r for r in dict.fromkeys(repo_list) if r not in excluded_set]
    log(f"{len(repo_list)} repo(s) to process, {len(excluded)} excluded")

    graphs, ok, skipped, failed = {}, [], [], []
    for r in repo_list:
        repo = Path(r)
        if not repo.is_dir():
            failed.append(f"{repo}  (folder not found)")
            continue
        try:
            graphs[repo] = build_repo(gfy, repo)
            ok.append(repo)
        except EmptyRepo as e:
            log(f"SKIP {repo}: {e}")
            skipped.append(f"{repo}  ({e})")
        except Exception as e:
            log(f"FAILED {repo}: {e}")
            failed.append(f"{repo}  ({e})")

    servers = {}
    if graphs and MERGE_GRAPHS:
        MERGED_GRAPH.parent.mkdir(parents=True, exist_ok=True)
        log(f"Merging {len(graphs)} graphs -> {MERGED_GRAPH}")
        run([gfy, "merge-graphs", *graphs.values(), "--out", MERGED_GRAPH])
        servers[MCP_SERVER_NAME] = {"command": mcp, "args": [str(MERGED_GRAPH)]}
    elif graphs:
        root = find_repos.ROOTS[0]
        for repo, graph in graphs.items():
            servers[per_repo_name(repo, root)] = {"command": mcp, "args": [str(graph)]}

    config_changed = False
    if servers and SETUP_CLAUDE_DESKTOP_MCP:
        config_changed = update_desktop_config(servers)
    if servers and SETUP_CLAUDE_CODE_MCP:
        update_claude_code_mcp(servers)

    dirty = {repo: graphify_git_changes(repo) for repo in ok}
    dirty = {k: v for k, v in dirty.items() if v}

    print()
    log(f"Done. OK: {len(ok)}  Skipped: {len(skipped)}  Failed: {len(failed)}  Excluded: {len(excluded)}")
    for label, items in (("Skipped", skipped), ("Failed", failed)):
        for i in items:
            print(f"   {label}: {i}")
    if dirty:
        print("\nThese repos still show Graphify changes in git (tracked files - commit or revert them):")
        for repo, files in dirty.items():
            print(f"   {repo}: {', '.join(files)}")
    if WRITE_GLOBAL_USAGE_RULES:
        write_usage_rules(GLOBAL_CLAUDE_MD)
    if NIGHTLY_REFRESH:
        register_nightly_task()
    if servers:
        print(f"\nMCP server(s): {', '.join(servers)}  (the server reloads the merged graph by itself)")
        if config_changed:
            print("Claude config changed: fully quit Claude (system tray > Quit) and reopen it.")


if __name__ == "__main__":
    main()
