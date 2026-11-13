"""
setup_token_savers.py - cheap token savers for Claude Code (Windows). Safe to re-run.

  1. Caps extended thinking (env MAX_THINKING_TOKENS in ~/.claude/settings.json).
  2. Adds a Haiku "researcher" subagent (~/.claude/agents/researcher.md) for
     search/read-heavy work: only its short summary comes back to the main thread.
  3. Adds the read_shunt.py hook: whole-file Reads over its MIN_LINES go to your
     local Ollama model for an outline + summary first (settings live in read_shunt.py).
  4. Lists the MCP servers Claude Code loads, so you can drop the ones you don't use
     (set REMOVE_MCP_SERVERS below and re-run).

settings.json is backed up before any change. Run:  python setup_token_savers.py
"""
from pathlib import Path

# ============================ SETTINGS ============================
CLAUDE_DIR = Path.home() / ".claude"

SET_THINKING_CAP = True
MAX_THINKING_TOKENS = 8000          # lower = cheaper; raise for hard debugging (or use /effort per session)

ADD_HAIKU_SUBAGENT = True
SUBAGENT_NAME = "researcher"
SUBAGENT_MODEL = "haiku"
SUBAGENT_TOOLS = "Read, Grep, Glob, Bash, WebSearch, WebFetch"

ADD_READ_SHUNT = True               # False = remove the hook
READ_SHUNT_SCRIPT = Path(__file__).resolve().parent / "read_shunt.py"
READ_SHUNT_TIMEOUT_S = 120

LIST_MCP_SERVERS = True
CLAUDE_EXE = ""                     # full path to claude.exe / claude.cmd if auto-detect fails
REMOVE_MCP_SERVERS = []             # e.g. ["some-server"] - removed from Claude Code (user scope) only
# ==================================================================

import json, os, shutil, subprocess, sys, time

SUBAGENT_BODY = """You are a read-only research helper. Find what was asked (code locations, call paths,
config values, log lines, web facts) and reply with a SHORT summary: file:line references,
key facts, max ~15 lines. Never paste whole files or long logs. Do not edit anything.
For code structure in Openflow repos, prefer the graphify MCP tools (pass project_path,
token_budget 500-1000) before reading raw files."""


def log(msg):
    print(f"[token-savers] {msg}")


def update_settings():
    path = CLAUDE_DIR / "settings.json"
    data = {}
    if path.exists():
        data = json.loads(path.read_text(encoding="utf-8") or "{}")
        backup = path.with_name(f"settings.backup-{time.strftime('%Y%m%d-%H%M%S')}.json")
        shutil.copy2(path, backup)
        log(f"backed up settings.json -> {backup.name}")

    if SET_THINKING_CAP:
        data.setdefault("env", {})["MAX_THINKING_TOKENS"] = str(MAX_THINKING_TOKENS)
        log(f"MAX_THINKING_TOKENS = {MAX_THINKING_TOKENS}")

    # read_shunt hook: drop any old entry, then add the current one
    pre = data.setdefault("hooks", {}).setdefault("PreToolUse", [])
    for group in pre:
        group["hooks"] = [h for h in group.get("hooks", []) if "read_shunt.py" not in h.get("command", "")]
    pre[:] = [g for g in pre if g.get("hooks")]
    if ADD_READ_SHUNT:
        cmd = f'"{sys.executable}" "{READ_SHUNT_SCRIPT}"'
        pre.append({"matcher": "Read|Bash",
                    "hooks": [{"type": "command", "command": cmd, "timeout": READ_SHUNT_TIMEOUT_S}]})
        log(f"read_shunt hook added: {cmd}")
    else:
        log("read_shunt hook removed")
    if not pre:
        del data["hooks"]["PreToolUse"]
    if not data["hooks"]:
        del data["hooks"]

    CLAUDE_DIR.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2), encoding="utf-8")


def add_subagent():
    path = CLAUDE_DIR / "agents" / f"{SUBAGENT_NAME}.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    desc = ("Use proactively for searching code, reading many files, digging through logs or "
            "web research. Returns a short summary instead of raw content.")
    path.write_text(
        f"---\nname: {SUBAGENT_NAME}\ndescription: {desc}\ntools: {SUBAGENT_TOOLS}\n"
        f"model: {SUBAGENT_MODEL}\n---\n\n{SUBAGENT_BODY}\n",
        encoding="utf-8")
    log(f"subagent written: {path}")


def claude_exe():
    if CLAUDE_EXE and Path(CLAUDE_EXE).exists():
        return CLAUDE_EXE
    p = shutil.which("claude") or shutil.which("claude.cmd")
    if p:
        return p
    dirs = [Path.home() / ".local" / "bin",
            Path(os.environ.get("APPDATA", "")) / "npm",
            Path(os.environ.get("LOCALAPPDATA", "")) / "Programs" / "claude",
            Path.home() / ".claude" / "local"]
    for d in dirs:
        for ext in (".exe", ".cmd", ""):
            f = d / ("claude" + ext)
            if f.exists():
                return str(f)
    return None


def mcp_servers():
    exe = claude_exe()
    if not exe:
        log("claude CLI not found - set CLAUDE_EXE (run `where claude` in a terminal) and re-run")
        return
    for name in REMOVE_MCP_SERVERS:
        r = subprocess.run([exe, "mcp", "remove", name, "-s", "user"], capture_output=True, text=True)
        log(f"remove {name}: {'ok' if r.returncode == 0 else r.stderr.strip()[:200]}")
    if LIST_MCP_SERVERS:
        r = subprocess.run([exe, "mcp", "list"], capture_output=True, text=True, timeout=120)
        log("MCP servers Claude Code loads:")
        print(r.stdout.strip() or r.stderr.strip())


def main():
    update_settings()
    if ADD_HAIKU_SUBAGENT:
        add_subagent()
    if LIST_MCP_SERVERS or REMOVE_MCP_SERVERS:
        mcp_servers()
    log("done - restart Claude Code sessions to pick up the changes")


if __name__ == "__main__":
    main()
