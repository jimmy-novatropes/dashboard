"""
Tower deploy helper for the AWS Error Feed - ONE STEP after any change. Run on the tower:
    python new_app\\deploy_tower.py        (from the dashboard folder; or double-click this file)
  1. checks the code compiles (stops here if it doesn't - the running dashboard is left alone)
  2. commits the changed app files locally (COMMIT); pushing to GitHub is off by default (PUSH) - push yourself
  3. downloads an extra Ollama model (MODEL_TO_INSTALL, skipped if empty or already installed)
  4. stops the running dashboard and starts it again (scheduled task "AWS Error Feed")
  5. waits until it answers and prints a short status (local + cloud model, tasks.db, daily intake, end of dashboard.log)
  Daily intake: installs its packages if missing; GMAIL_SIGN_IN once for Gmail; INTAKE_RUN_NOW to test it.
  6. says whether Claude must be restarted (only when mcp_server.py changed since the last run)
"""
import json
import os
import subprocess
import sys
import time
import urllib.parse
import urllib.request

# ------------------------------------------------------------------ settings
COMMIT = True                           # commit COMMIT_PATHS locally (if changed)
PUSH = False                            # True = also git pull --rebase + git push to GitHub
COMMIT_MESSAGE = ""                     # "" = automatic ("Update: <changed files>"); or write your own
COMMIT_PATHS = [".gitignore", "CLAUDE.md", "new_app", "old_app", "logging_kit"]   # never config.json / *.db (git-ignored)
COMMIT_TRAILER = ("Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>\n"
                  "Claude-Session: https://claude.ai/code/session_011KCTv4MMSvGb1qCJvzuc34")
CHECK_FILES = ["aws_error_feed.py", "aws_tasks.py", "aws_local.py", "aws_ops.py", "aws_health.py",
               "aws_prefs.py", "aws_infra.py", "aws_intake.py", "aws_brief.py", "aws_research.py", "aws_updates.py", "mcp_server.py"]
GMAIL_SIGN_IN = False                   # True ONCE: opens the browser to allow read-only Gmail for the daily intake
INTAKE_RUN_NOW = False                  # True = run the daily intake for yesterday after the restart (to test it)
INTAKE_PACKAGES = {"google.auth": "google-auth", "google_auth_oauthlib": "google-auth-oauthlib",
                   "requests": "requests", "pypdf": "pypdf"}   # import name -> pip name, installed if missing
MODEL_TO_INSTALL = ""                   # e.g. "qwen3:30b-a3b"; "" = don't download anything
SWITCH_TO_NEW_MODEL = False             # True = make it the active model after the restart
NEW_MODEL_CONTEXT = 8192                # context used if SWITCH_TO_NEW_MODEL is True
OLLAMA_URL = "http://127.0.0.1:11434"
DASHBOARD_URL = "http://127.0.0.1:8766"
TASK_NAME = "AWS Error Feed"
WAIT_FOR_DASHBOARD_SECONDS = 180

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
LOG = os.path.join(HERE, "dashboard.log")
STAMP = os.path.join(HERE, "last_deploy.json")      # git-ignored: connector hash at the last run


def say(t):
    print(f"\n==> {t}", flush=True)


def get_json(url, timeout=10):
    with urllib.request.urlopen(url, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


def run(cmd, cwd=None):
    r = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, shell=isinstance(cmd, str))
    out = (r.stdout + r.stderr).strip()
    return r.returncode, out


# ------------------------------------------------------------------ 1. compile check
def compile_check():
    say("Checking the code compiles")
    bad = []
    for f in CHECK_FILES:
        path = os.path.join(HERE, f)
        if not os.path.exists(path):
            continue
        code, out = run([sys.executable, "-m", "py_compile", path])
        if code:
            bad.append(f"{f}: {out.splitlines()[-1] if out else 'error'}")
    print("\n".join("!! " + b for b in bad) if bad else "ok")
    return not bad


# ------------------------------------------------------------------ 2. git commit + push
def commit_and_push():
    say("Committing the changes")
    paths = [p for p in COMMIT_PATHS if os.path.exists(os.path.join(REPO, p))]
    code, out = run(["git", "add", "--"] + paths, cwd=REPO)
    if code:
        print("!! git add failed:", out)
        return
    code, staged = run(["git", "diff", "--cached", "--name-only"], cwd=REPO)
    if not staged:
        print("nothing new to commit")
    else:
        print(staged)
        files = [os.path.basename(f) for f in staged.splitlines()]
        auto = "Update: " + ", ".join(sorted(set(files))[:8]) + (" ..." if len(set(files)) > 8 else "")
        msg = (COMMIT_MESSAGE or auto) + ("\n\n" + COMMIT_TRAILER if COMMIT_TRAILER else "")
        code, out = run(["git", "commit", "-m", msg], cwd=REPO)
        print(out.splitlines()[0] if out else "")
        if code:
            print("!! git commit failed - commit it yourself (the rest continues).")
            return
    if PUSH:
        push()


def push():
    if True:                            # get the laptop's commits first
        say("Getting the laptop's commits (git pull --rebase)")
        code, out = run(["git", "pull", "--rebase", "--autostash"], cwd=REPO)
        print(out or "ok")
        if code:
            run(["git", "rebase", "--abort"], cwd=REPO)
            print("!! pull failed (conflict?) - left as it was; sort it out with git, then run this again.")
            return
    say("Pushing the tower's commits to GitHub")
    code, out = run(["git", "push"], cwd=REPO)
    print(out or "(nothing to push)")
    if code:
        print("!! git push failed - run `git push` yourself in the dashboard folder (the rest continues).")


# ------------------------------------------------------------------ 3. model download
def install_model(name):
    say(f"Model {name}")
    try:
        have = [m["name"] for m in get_json(OLLAMA_URL + "/api/tags").get("models", [])]
    except Exception as ex:
        print(f"!! Ollama isn't reachable at {OLLAMA_URL} ({ex}). Start Ollama and run this again.")
        return False
    if name in have or name + ":latest" in have:
        print("already installed - skipping the download")
        return True
    print("downloading (about 19 GB for qwen3:30b-a3b - this can take a while)...")
    req = urllib.request.Request(OLLAMA_URL + "/api/pull", data=json.dumps({"model": name, "stream": True}).encode(),
                                 headers={"Content-Type": "application/json"})
    last, status = -10, ""
    try:
        with urllib.request.urlopen(req, timeout=4 * 3600) as r:
            for line in r:
                try:
                    d = json.loads(line)
                except ValueError:
                    continue
                if d.get("error"):
                    print("!! download failed:", d["error"])
                    return False
                if d.get("total"):
                    pct = int(100 * (d.get("completed") or 0) / d["total"])
                    if pct >= last + 5:
                        print(f"   {pct}%", flush=True)
                        last = pct
                elif d.get("status") and d["status"] != status:
                    status = d["status"]
                    print("  ", status, flush=True)
    except Exception as ex:
        print("!! download failed:", ex)
        return False
    print("done")
    return True


# ------------------------------------------------------------------ 3b. daily intake (Gmail + Fireflies)
def intake_setup():
    import importlib
    missing = []
    for mod, pkg in INTAKE_PACKAGES.items():
        try:
            importlib.import_module(mod)
        except ImportError:
            missing.append(pkg)
    if missing:
        say("Installing packages for the daily intake: " + ", ".join(missing))
        code, out = run([sys.executable, "-m", "pip", "install", "--quiet"] + missing)
        print(out.splitlines()[-1] if out else "ok")
    if GMAIL_SIGN_IN:
        say("Gmail sign-in: a browser window opens - pick your account and allow read-only access")
        try:
            sys.path.insert(0, HERE)
            import aws_intake
            aws_intake.gmail_sign_in(HERE)
            print("signed in - gmail_token.json saved (set GMAIL_SIGN_IN back to False)")
        except Exception as ex:
            print("!! Gmail sign-in failed:", ex)


def intake_run_now():
    say("Running the daily intake for yesterday")
    try:
        r = get_json(DASHBOARD_URL + "/api/tasks/intake/run", timeout=30)
        if r.get("error"):
            print("!!", r["error"])
            return
        t0 = time.time()
        while time.time() - t0 < 900:
            time.sleep(10)
            st = get_json(DASHBOARD_URL + "/api/tasks/intake")
            if not st.get("busy"):
                break
        last = (st.get("runs") or [{}])[0]
        print(f"{last.get('day')}: {last.get('emails')} emails, {last.get('meetings')} meetings -> "
              f"{last.get('added')} proposed task(s)" + (f"\n!! {last['error']}" if last.get("error") else ""))
    except Exception as ex:
        print("!! intake run failed:", ex)


# ------------------------------------------------------------------ 4. restart the dashboard
def restart_dashboard():
    say("Stopping the dashboard")
    ps = ("Get-CimInstance Win32_Process -Filter \"name='pythonw.exe' or name='python.exe'\" | "
          "Where-Object { $_.CommandLine -match 'aws_error_feed' } | "
          "ForEach-Object { Stop-Process -Id $_.ProcessId -Force; $_.ProcessId }")
    code, out = run(["powershell", "-NoProfile", "-Command", ps])
    print(f"stopped process(es): {out.replace(os.linesep, ', ')}" if out else "it wasn't running")
    time.sleep(3)
    say(f"Starting the scheduled task \"{TASK_NAME}\"")
    code, out = run(["schtasks", "/Run", "/TN", TASK_NAME])
    print(out)
    if code:
        print("!! couldn't start the task - run install_autostart.ps1 again, or start it in Task Scheduler.")
        return False
    return True


def wait_for_dashboard():
    say("Waiting for the dashboard to answer")
    t0 = time.time()
    while time.time() - t0 < WAIT_FOR_DASHBOARD_SECONDS:
        try:
            st = get_json(DASHBOARD_URL + "/api/ai/status", timeout=5)
            print(f"up after {time.time() - t0:.0f}s - local model: {st.get('model')} ({st.get('detail')})")
            return True
        except Exception:
            time.sleep(3)
    print(f"!! no answer after {WAIT_FOR_DASHBOARD_SECONDS}s - see the end of dashboard.log below")
    return False


def switch_model(name, ctx):
    say(f"Switching the dashboard to {name} (context {ctx})")
    try:
        r = get_json(f"{DASHBOARD_URL}/api/ai/model?name={urllib.parse.quote(name)}&num_ctx={ctx}", timeout=30)
        print(r.get("error") or r.get("message"))
    except Exception as ex:
        print("!! couldn't switch:", ex, "- use the picker at the top of the Health tab")


def claude_restart_needed():
    """True when mcp_server.py (the Claude connector) changed since the last run of this script."""
    import hashlib
    try:
        with open(os.path.join(HERE, "mcp_server.py"), "rb") as f:
            h = hashlib.sha1(f.read()).hexdigest()
    except OSError:
        return False
    try:
        with open(STAMP) as f:
            old = json.load(f).get("mcp_sha")
    except (OSError, ValueError):
        old = None
    try:
        with open(STAMP, "w") as f:
            json.dump({"mcp_sha": h, "at": time.strftime("%Y-%m-%d %H:%M")}, f)
    except OSError:
        pass
    return old is not None and old != h


def report():
    say("Status")
    try:
        st = get_json(DASHBOARD_URL + "/api/ai/status")
        c = st.get("cloud") or {}
        print("local model  :", st.get("model"), "-", st.get("detail"))
        print("cloud model  :", (f"{c.get('model')} ON - today {c.get('calls', 0)} calls, {c.get('tokens', 0):,} of "
                                 f"{c.get('daily_token_cap', 0):,} tokens, {c.get('fallbacks', 0)} fallbacks")
                                if c.get("enabled") else "off (cloud_llm in config.json)")
    except Exception as ex:
        print("status not answering:", ex)
    try:
        m = get_json(DASHBOARD_URL + "/api/ai/models")
        print("model in use :", m["current"], f"(context {m['num_ctx']}, set in {m['source']})")
        print("installed    :", ", ".join(f"{x['name']} ({x['size_gb']} GB)" for x in m["installed"]))
    except Exception as ex:
        print("model picker not answering:", ex)
    try:
        t = get_json(DASHBOARD_URL + "/api/tasks?status=all")
        if t.get("error"):
            print("tasks        : !!", t["error"])
        else:
            n = len([i for i in t["items"] if i["status"] != "done"])
            print(f"tasks        : tasks.db ok - {n} not done, {len(t['items'])} total")
    except Exception as ex:
        print("tasks        : !! not answering:", ex, "(old version still running?)")
    try:
        p = get_json(DASHBOARD_URL + "/api/prefs")
        print("priorities   :", ", ".join(f"{x['profile']}={x['priority']}" for x in p["profiles"]))
    except Exception as ex:
        print("priorities not answering:", ex)
    try:
        s = get_json(DASHBOARD_URL + "/api/tasks/intake")
        last = (s.get("runs") or [{}])[0]
        print("daily intake :", (f"on at {s['run_at']} - Gmail {'signed in' if s['gmail'] else 'NOT signed in'}, "
                                 f"Fireflies {'key set' if s['fireflies'] else 'NO key'}" if s.get("enabled")
                                 else "off (intake.enabled in config.json)")
              + (f"; last {last.get('day')}: {last.get('added')} proposed" if last.get("day") else ""))
    except Exception as ex:
        print("daily intake : not answering:", ex)
    try:
        with open(LOG, encoding="utf-8", errors="replace") as f:
            tail = f.readlines()[-12:]
        print("\n--- end of dashboard.log ---\n" + "".join(tail).rstrip())
    except OSError:
        pass


if __name__ == "__main__":
    if not compile_check():
        say("Stopped: fix the errors above first. The running dashboard was not touched.")
        if sys.stdin and sys.stdin.isatty():
            input("\nPress Enter to close...")
        sys.exit(1)
    if COMMIT:
        commit_and_push()
    model_ok = install_model(MODEL_TO_INSTALL) if MODEL_TO_INSTALL else False
    intake_setup()
    if restart_dashboard():
        up = wait_for_dashboard()
        if up and SWITCH_TO_NEW_MODEL and model_ok:
            switch_model(MODEL_TO_INSTALL, NEW_MODEL_CONTEXT)
        if up and INTAKE_RUN_NOW:
            intake_run_now()
    report()
    if claude_restart_needed():
        say("Done. RESTART CLAUDE: YES - the connector changed. After you push: on the laptop `git pull` in the "
            "dashboard folder, then quit Claude (tray icon -> Quit) and reopen it; same for Claude on the tower.")
    else:
        say("Done. RESTART CLAUDE: NO - the connector didn't change.")
    if sys.stdin and sys.stdin.isatty():
        input("\nPress Enter to close...")
