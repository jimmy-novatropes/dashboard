"""
Tower deploy helper for the AWS Error Feed:
  1. (off by default) pushes the tower's commits to GitHub
  2. downloads an extra Ollama model (skipped if it's already installed)
  3. stops the running dashboard and starts it again (scheduled task "AWS Error Feed")
  4. waits until it answers and prints a short status

Run on the tower from the dashboard folder:
    python new_app\\deploy_tower.py
Afterwards: fully quit Claude (tray icon -> Quit) and reopen it, so the connector reloads.
"""
import json
import os
import subprocess
import sys
import time
import urllib.parse
import urllib.request

# ------------------------------------------------------------------ settings
PUSH_TO_GITHUB = False                  # already pushed; set True to push from here
MODEL_TO_INSTALL = "qwen3:30b-a3b"      # "" = don't download anything
SWITCH_TO_NEW_MODEL = False             # True = make it the active model after the restart
NEW_MODEL_CONTEXT = 8192                # context used if SWITCH_TO_NEW_MODEL is True
OLLAMA_URL = "http://127.0.0.1:11434"
DASHBOARD_URL = "http://127.0.0.1:8766"
TASK_NAME = "AWS Error Feed"
WAIT_FOR_DASHBOARD_SECONDS = 180

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
LOG = os.path.join(HERE, "dashboard.log")


def say(t):
    print(f"\n==> {t}", flush=True)


def get_json(url, timeout=10):
    with urllib.request.urlopen(url, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


def run(cmd, cwd=None):
    r = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, shell=isinstance(cmd, str))
    out = (r.stdout + r.stderr).strip()
    return r.returncode, out


# ------------------------------------------------------------------ 1. git push
def push():
    say("Pushing the tower's commits to GitHub")
    code, out = run(["git", "push"], cwd=REPO)
    print(out or "(nothing to push)")
    if code:
        print("!! git push failed - run `git push` yourself in the dashboard folder (the rest continues).")


# ------------------------------------------------------------------ 2. model download
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


# ------------------------------------------------------------------ 3. restart the dashboard
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


def report():
    say("Status")
    try:
        m = get_json(DASHBOARD_URL + "/api/ai/models")
        print("model in use :", m["current"], f"(context {m['num_ctx']}, set in {m['source']})")
        print("installed    :", ", ".join(f"{x['name']} ({x['size_gb']} GB)" for x in m["installed"]))
    except Exception as ex:
        print("model picker not answering:", ex)
    try:
        p = get_json(DASHBOARD_URL + "/api/prefs")
        print("priorities   :", ", ".join(f"{x['profile']}={x['priority']}" for x in p["profiles"]))
    except Exception as ex:
        print("priorities not answering:", ex)
    try:
        with open(LOG, encoding="utf-8", errors="replace") as f:
            tail = f.readlines()[-12:]
        print("\n--- end of dashboard.log ---\n" + "".join(tail).rstrip())
    except OSError:
        pass


if __name__ == "__main__":
    if PUSH_TO_GITHUB:
        push()
    model_ok = install_model(MODEL_TO_INSTALL) if MODEL_TO_INSTALL else False
    if restart_dashboard():
        up = wait_for_dashboard()
        if up and SWITCH_TO_NEW_MODEL and model_ok:
            switch_model(MODEL_TO_INSTALL, NEW_MODEL_CONTEXT)
    report()
    say("Done. Now fully quit Claude (tray icon -> Quit) and reopen it so the connector reloads.")
    if sys.stdin and sys.stdin.isatty():
        input("\nPress Enter to close...")
