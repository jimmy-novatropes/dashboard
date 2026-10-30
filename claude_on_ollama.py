"""Claude Code on Ollama - the backup for when the Claude plan's limit is used up.

Double-click this file (or run: python claude_on_ollama.py) on the tower or the laptop.
It opens Claude Code in a terminal, but every request goes to Ollama (your Ollama Pro cloud models),
so it does NOT use your Claude limit. Only this window is switched; normal Claude stays on your plan.
Needs: Ollama installed and running, and `ollama signin` done once.
"""
import json
import os
import shutil
import subprocess
import sys
import urllib.request

# ------------------------------------------------------------------ settings
MODEL          = "glm-5.3:cloud"         # main model (coding / agent work). Cheaper: "gpt-oss:120b-cloud"
FAST_MODEL     = "gpt-oss:120b-cloud"    # Claude Code's small background calls (titles, summaries)
FALLBACK_MODEL = "gpt-oss:120b-cloud"    # used when MODEL can't be pulled
OLLAMA_URL     = "http://127.0.0.1:11434"
WORK_DIR       = ""                      # "" = the "Openflow Repos" folder (the folder above dashboard)
ADD_DASHBOARD_TOOLS = True               # add the aws-error-feed tools (ask_tower, find_code, code_change...) once
FEED_URL       = "https://initium.tail11109.ts.net,http://127.0.0.1:8766"   # dashboard, tried in this order
INSTALL_CLAUDE_CODE = 'powershell -NoProfile -ExecutionPolicy Bypass -Command "irm https://claude.ai/install.ps1 | iex"'
# ----------------------------------------------------------------------------

DASH  = os.path.dirname(os.path.abspath(__file__))
REPOS = os.path.dirname(DASH)
WORK  = WORK_DIR or REPOS


def say(text):
    print(text, flush=True)


def stop(text):
    say("\n" + text)
    input("Press Enter to close")
    sys.exit(1)


def get_json(path, timeout=10):
    with urllib.request.urlopen(OLLAMA_URL + path, timeout=timeout) as r:
        return json.load(r)


def have_model(m):
    names = [x.get("name", "") for x in get_json("/api/tags").get("models", [])]
    return m in names or m + ":latest" in names


def pull(m):
    say(f"Getting {m} ...")
    subprocess.call(["ollama", "pull", m])
    return have_model(m)


def find_claude():
    exe = shutil.which("claude")
    if exe:
        return exe
    local_bin = os.path.join(os.path.expanduser("~"), ".local", "bin")
    os.environ["PATH"] += os.pathsep + local_bin
    return shutil.which("claude")


def main():
    global MODEL
    # 1. Ollama running?
    try:
        say(f"Ollama {get_json('/api/version', 5).get('version')} at {OLLAMA_URL}")
    except Exception:
        stop(f"Ollama isn't running at {OLLAMA_URL}.\n"
             "Install it from https://ollama.com/download, open it, run `ollama signin`, then run this again.")

    # 2. Models (cloud models are a tiny download; they need `ollama signin`)
    if not have_model(MODEL) and not pull(MODEL):
        say(f"Couldn't get {MODEL} - using {FALLBACK_MODEL} instead. (Signed in? Run: ollama signin)")
        MODEL = FALLBACK_MODEL
    for m in {MODEL, FAST_MODEL}:
        if not have_model(m) and not pull(m):
            stop(f"Couldn't get {m}. Run `ollama signin` (Ollama Pro account) and try again.")

    # 3. Claude Code installed?
    claude = find_claude()
    if not claude:
        if input("Claude Code isn't installed. Install it now? (Y/n) ").strip().lower().startswith("n"):
            stop("Install it later with: " + INSTALL_CLAUDE_CODE)
        subprocess.call(INSTALL_CLAUDE_CODE, shell=True)
        claude = find_claude()
        if not claude:
            stop("Claude Code still not found - open a new window and run this again.")

    # 4. Dashboard tools (once, for every Claude Code session on this computer)
    if ADD_DASHBOARD_TOOLS:
        have = subprocess.call([claude, "mcp", "get", "aws-error-feed"],
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL) == 0
        if not have:
            say("Adding the dashboard tools (aws-error-feed) to Claude Code...")
            subprocess.call([claude, "mcp", "add", "aws-error-feed", "--scope", "user",
                             "-e", f"AWS_ERROR_FEED_URL={FEED_URL}",
                             "-e", f"AWS_ERROR_FEED_REPOS={REPOS}",
                             "-e", "AWS_ERROR_FEED_AUTOSTART=0",
                             "--", sys.executable, os.path.join(DASH, "new_app", "mcp_server.py")])

    # 5. Point this window's Claude Code at Ollama
    env = dict(os.environ)
    env.pop("ANTHROPIC_API_KEY", None)
    env.update({
        "ANTHROPIC_BASE_URL": OLLAMA_URL,
        "ANTHROPIC_AUTH_TOKEN": "ollama",
        "ANTHROPIC_MODEL": MODEL,
        "ANTHROPIC_DEFAULT_OPUS_MODEL": MODEL,
        "ANTHROPIC_DEFAULT_SONNET_MODEL": MODEL,
        "ANTHROPIC_DEFAULT_HAIKU_MODEL": FAST_MODEL,
        "ANTHROPIC_SMALL_FAST_MODEL": FAST_MODEL,
        "CLAUDE_CODE_SUBAGENT_MODEL": MODEL,
        "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1",
        "API_TIMEOUT_MS": "600000",
    })
    if os.name == "nt":
        os.system(f"title Claude Code on Ollama ({MODEL})")
    say(f"\nClaude Code on Ollama - model {MODEL} (background: {FAST_MODEL})")
    say(f"Folder: {WORK}")
    say("Not using your Claude limit. Keep tasks small and check every diff.\n")
    # Own console window on Windows, so Claude Code always gets a real keyboard (not "--print" mode)
    flags = subprocess.CREATE_NEW_CONSOLE if os.name == "nt" else 0
    code = subprocess.call([claude] + sys.argv[1:], env=env, cwd=WORK, creationflags=flags)
    if code != 0:
        input(f"Claude Code exited with code {code}. Press Enter to close")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        pass
    except Exception as e:
        stop(f"Error: {e}")
