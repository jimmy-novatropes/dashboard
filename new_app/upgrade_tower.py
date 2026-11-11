"""
Tower upgrade + model bake-off for the AWS Error Feed - ONE STEP. Run on the tower:
    python new_app\\upgrade_tower.py        (from the dashboard folder; or double-click this file)
  1. makes sure Ollama is running (starts it if not)
  2. checks the code compiles (stops here if it doesn't - the running dashboard is left alone)
  3. downloads the candidate models (local + cloud; a missing tag is skipped, not fatal)
  4. commits locally and restarts the dashboard (same as deploy_tower.py)
  5. runs the triage eval on today's setup and on every candidate (same labeled patterns for all)
  6. applies the winners: a cloud model that beats today's verifier by MIN_GAIN_POINTS becomes the verifier
     (cloud_llm.kind_models in config.json, backup kept); a local model that beats today's local model and
     isn't much slower becomes the local model. Then restarts once more and prints the status.
  Results: new_app/upgrade_results.json (git-ignored). Re-run any time - downloads already done are skipped.
"""
import json
import os
import shutil
import subprocess
import sys
import time
import urllib.parse
import urllib.request

# ------------------------------------------------------------------ settings
CLOUD_CANDIDATES = ["glm-5.3:cloud", "deepseek-v4.1-flash:cloud", "kimi-k3.1-pro:cloud"]   # Ollama Pro cloud models
LOCAL_CANDIDATES = ["gemma4:26b-a4b", "gpt-oss:20b"]   # backup models that suit the 12 GB RTX A2000 (MoE / small)
APPLY_TO_KINDS = ["triage_verify"]       # which steps the winning cloud model takes over ("triage" too = all triage)
MIN_GAIN_POINTS = 3.0                    # accuracy points a candidate must win by to be applied
MAX_ERRORS = 2                           # candidates with more failed calls than this are not applied
MAX_UNCLEAR_PCT = 25                     # more "unclear" verdicts than this = the model didn't answer properly
MAX_SLOWER_PCT = 50                      # a local candidate may be at most this much slower than today's local model
RUN_LOCAL_EVALS = True                   # False = skip the local bake-off (it takes longest: ~20-60 min per model)
APPLY_WINNERS = True                     # False = only print the comparison, change nothing
COMMIT = True                            # commit the app files locally before restarting (never pushes)
LOCAL_CONTEXT = 16384                    # context for a newly chosen local model
EVAL_TIMEOUT_MIN_CLOUD = 30
EVAL_TIMEOUT_MIN_LOCAL = 90
OLLAMA_URL = "http://127.0.0.1:11434"
DASHBOARD_URL = "http://127.0.0.1:8766"
OLLAMA_START_WAIT_S = 90

HERE = os.path.dirname(os.path.abspath(__file__))
CONFIG = os.path.join(HERE, "config.json")
RESULTS = os.path.join(HERE, "upgrade_results.json")
sys.path.insert(0, HERE)
import deploy_tower as dt                # reuse: compile check, commit, model download, restart, status


def say(t):
    print(f"\n==> {t}", flush=True)


def get_json(url, timeout=30):
    with urllib.request.urlopen(url, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


# ------------------------------------------------------------------ 1. Ollama
def ollama_up():
    try:
        get_json(OLLAMA_URL + "/api/version", timeout=3)
        return True
    except Exception:
        return False


def ensure_ollama():
    say("Ollama")
    if ollama_up():
        print("running")
        return True
    local = os.environ.get("LOCALAPPDATA", "")
    app = os.path.join(local, "Programs", "Ollama", "ollama app.exe")
    cli = os.path.join(local, "Programs", "Ollama", "ollama.exe")
    print("not running - starting it")
    try:
        if os.path.exists(app):
            subprocess.Popen([app])
        elif os.path.exists(cli):
            subprocess.Popen([cli, "serve"], creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        else:
            subprocess.Popen(["ollama", "serve"], creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    except OSError as ex:
        print("!! couldn't start Ollama:", ex)
    t0 = time.time()
    while time.time() - t0 < OLLAMA_START_WAIT_S:
        if ollama_up():
            print(f"up after {time.time() - t0:.0f}s")
            return True
        time.sleep(2)
    print("!! Ollama didn't start - install it from ollama.com (or start it from the Start menu) and run this again.")
    return False


# ------------------------------------------------------------------ 3. downloads
def download(models):
    ok = []
    for m in models:
        if dt.install_model(m):
            ok.append(m)
        else:
            print(f"   skipping {m} (not available)")
    return ok


# ------------------------------------------------------------------ 5. evals
def run_eval(target, timeout_min):
    print(f"   {target} ...", end="", flush=True)
    t0 = time.time()
    while True:
        r = get_json(DASHBOARD_URL + "/api/quality/eval?" + urllib.parse.urlencode({"target": target}))
        if r.get("error"):
            print(" !!", r["error"])
            return None
        jid = r["id"]
        j = {}
        while time.time() - t0 < timeout_min * 60:
            time.sleep(5)
            j = get_json(DASHBOARD_URL + "/api/ai/job?id=" + jid)
            if j.get("status") != "running":
                break
        if j.get("status") == "running":
            print(f" !! still running after {timeout_min} min - skipped")
            return None
        answer = str(j.get("answer") or j.get("error") or "")
        if answer.startswith("An eval is already running") and time.time() - t0 < timeout_min * 60:
            time.sleep(60)                      # the weekly eval is running: wait for it and try again
            continue
        if answer.startswith("No labeled patterns"):
            print(" !!", answer)
            return None
        break
    rows = get_json(DASHBOARD_URL + "/api/quality/evals?n=1").get("rows") or []
    if not rows or rows[0].get("target") != target:
        print(" !! no result:", answer[:200])
        return None
    d = rows[0]
    print(f" accuracy {d['accuracy_pct']}%  ({d['cases']} cases, {d.get('secs', '?')}s)")
    return d


def table(results):
    say("Comparison (same labeled patterns for every row)")
    print(f"{'target':40} {'accuracy':>8} {'precision':>9} {'recall':>7} {'unclear':>7} {'errors':>6} {'secs':>6}")
    for t, d in results.items():
        if d:
            print(f"{t:40} {d['accuracy_pct']:>7}% {str(d['precision_pct']):>8}% {str(d['recall_pct']):>6}% "
                  f"{d['unclear']:>7} {d['errors']:>6} {str(d.get('secs', '')):>6}"
                  + ("   <- mostly no valid answer, not a fair score" if d["unclear"] > d["cases"] * MAX_UNCLEAR_PCT / 100 else ""))
        else:
            print(f"{t:40} {'(failed)':>8}")


def best(results, prefix, baseline):
    base = results.get(baseline)
    cands = [(t, d) for t, d in results.items() if t.startswith(prefix) and d and d["errors"] <= MAX_ERRORS
             and d["unclear"] <= d["cases"] * MAX_UNCLEAR_PCT / 100]
    if not base or not cands:
        return None, base
    t, d = max(cands, key=lambda x: (x[1]["accuracy_pct"], -x[1]["errors"], -(x[1].get("secs") or 0)))
    return (t, d) if d["accuracy_pct"] >= base["accuracy_pct"] + MIN_GAIN_POINTS else None, base


# ------------------------------------------------------------------ 6. apply
def write_config(update):
    with open(CONFIG, "rb") as f:
        raw = f.read()
    bom = raw.startswith(b"\xef\xbb\xbf")
    cfg = json.loads(raw.decode("utf-8-sig"))
    update(cfg)
    backup = CONFIG + ".bak-" + time.strftime("%Y%m%d-%H%M%S")
    shutil.copy2(CONFIG, backup)
    with open(CONFIG, "w", encoding="utf-8-sig" if bom else "utf-8") as f:
        json.dump(cfg, f, indent=2)
    print(f"config.json updated (backup: {os.path.basename(backup)})")


def pause():
    if sys.stdin and sys.stdin.isatty():
        input("\nPress Enter to close...")


if __name__ == "__main__":
    if not ensure_ollama():
        pause(); sys.exit(1)
    if not dt.compile_check():
        say("Stopped: fix the errors above first. The running dashboard was not touched.")
        pause(); sys.exit(1)
    say("Downloading candidate models (already installed ones are skipped)")
    cloud_ok = download(CLOUD_CANDIDATES)
    local_ok = download(LOCAL_CANDIDATES) if RUN_LOCAL_EVALS else []
    if COMMIT:
        dt.commit_and_push()
    if not (dt.restart_dashboard() and dt.wait_for_dashboard()):
        dt.report(); pause(); sys.exit(1)
    time.sleep(20)                              # let the dashboard settle before the evals

    say("Triage eval: today's setup, then each candidate")
    results = {"cloud": run_eval("cloud", EVAL_TIMEOUT_MIN_CLOUD)}
    for m in cloud_ok:
        results["cloud:" + m] = run_eval("cloud:" + m, EVAL_TIMEOUT_MIN_CLOUD)
    if RUN_LOCAL_EVALS:
        results["local"] = run_eval("local", EVAL_TIMEOUT_MIN_LOCAL)
        for m in local_ok:
            results["local:" + m] = run_eval("local:" + m, EVAL_TIMEOUT_MIN_LOCAL)
    table(results)

    changed, notes = False, []
    win_cloud, base = best(results, "cloud:", "cloud")
    if win_cloud:
        model = win_cloud[0].split(":", 1)[1]
        notes.append(f"cloud: {model} wins ({win_cloud[1]['accuracy_pct']}% vs {base['accuracy_pct']}%) -> "
                     f"now used for {', '.join(APPLY_TO_KINDS)}")
        if APPLY_WINNERS:
            write_config(lambda c: c.setdefault("cloud_llm", {}).setdefault("kind_models", {}).update(
                {k: model for k in APPLY_TO_KINDS}))
            changed = True
    else:
        notes.append(f"cloud: no candidate beat today's setup by {MIN_GAIN_POINTS} points - nothing changed")
    if RUN_LOCAL_EVALS:
        win_local, lbase = best(results, "local:", "local")
        if win_local and lbase and (win_local[1].get("secs") or 0) > (lbase.get("secs") or 0) * (1 + MAX_SLOWER_PCT / 100):
            notes.append(f"local: {win_local[0]} is more accurate but too slow ({win_local[1].get('secs')}s vs "
                         f"{lbase.get('secs')}s) - not switched")
            win_local = None
        if win_local:
            model = win_local[0].split(":", 1)[1]
            notes.append(f"local: {model} wins ({win_local[1]['accuracy_pct']}% vs {lbase['accuracy_pct']}%) -> local backup model")
        elif not any(n.startswith("local:") for n in notes):
            notes.append(f"local: no candidate beat today's local model by {MIN_GAIN_POINTS} points - nothing changed")
    else:
        win_local = None

    if changed:
        say("Restarting with the new settings")
        dt.restart_dashboard() and dt.wait_for_dashboard()
    if win_local and APPLY_WINNERS:
        dt.switch_model(win_local[0].split(":", 1)[1], LOCAL_CONTEXT)

    with open(RESULTS, "w", encoding="utf-8") as f:
        json.dump({"at": time.strftime("%Y-%m-%d %H:%M"), "results": results, "decisions": notes}, f, indent=2)
    dt.report()
    say("Decisions")
    print("\n".join("- " + n for n in notes))
    if dt.claude_restart_needed():
        say("RESTART CLAUDE: YES - the connector changed (push, git pull on the laptop, quit + reopen Claude).")
    else:
        say("RESTART CLAUDE: NO")
    pause()
