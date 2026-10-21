"""
Per-project priority and dashboard-picked settings for the AWS Error Feed.

Priority per profile (client/project):
  high       - star: always first (Clients tab, overview, digest) and triaged first.
  normal     - as before.
  on_demand  - asleep: not polled, not scanned, not triaged and left out of "all clients" views until
               you open it (pick it in the dashboard, or ask Claude about it with profile=...). It then
               loads the last 24h and stays awake for on_demand_wake_hours (default 2), then sleeps again.

Settings picked in the dashboard (e.g. the local model) are kept in errors.db (meta table) and
win over config.json until changed again.
"""
import json
import re
import threading
import time
import urllib.request

PRIORITIES = ("high", "normal", "on_demand")
RANK = {"high": 0, "normal": 1, "on_demand": 2}


class Prefs:
    def __init__(self, store, cfg):
        self.store, self.cfg = store, cfg
        self.wake_hours = float(cfg.get("on_demand_wake_hours") or 2)
        self.on_wake = None                     # callback(profile) when an asleep profile is woken
        self.lock = threading.Lock()
        with store.lock:
            store.db.execute("CREATE TABLE IF NOT EXISTS profile_prefs(profile TEXT PRIMARY KEY, priority TEXT, "
                             "wake_until INTEGER, at INTEGER)")
            store.db.commit()
            rows = store.db.execute("SELECT profile, priority, wake_until FROM profile_prefs").fetchall()
        self.p = {r[0]: {"priority": r[1], "wake_until": r[2] or 0} for r in rows}
        for prof, pr in (cfg.get("profile_priority") or {}).items():   # config defaults for new profiles
            if prof not in self.p and pr in PRIORITIES:
                self.p[prof] = {"priority": pr, "wake_until": 0}

    # ---- priority
    def priority(self, profile):
        return (self.p.get(profile) or {}).get("priority") or "normal"

    def rank(self, profile):
        return RANK[self.priority(profile)]

    def _save(self, profile):
        d = self.p[profile]
        with self.store.lock:
            self.store.db.execute("INSERT OR REPLACE INTO profile_prefs VALUES (?,?,?,?)",
                                  (profile, d["priority"], d["wake_until"], int(time.time() * 1000)))
            self.store.db.commit()

    def set_priority(self, profile, priority):
        if priority not in PRIORITIES:
            raise ValueError(f"priority must be one of {PRIORITIES}")
        with self.lock:
            d = self.p.setdefault(profile, {"priority": "normal", "wake_until": 0})
            d["priority"] = priority
            if priority == "on_demand":
                d["wake_until"] = 0                 # goes to sleep now
            self._save(profile)

    # ---- on demand
    def asleep(self, profile):
        d = self.p.get(profile)
        return bool(d) and d["priority"] == "on_demand" and time.time() * 1000 > (d["wake_until"] or 0)

    def asleep_profiles(self):
        return sorted(p for p in self.p if self.asleep(p))

    def wake(self, profile, hours=None):
        """-> True if it was asleep (it now loads the last 24h)."""
        if self.priority(profile) != "on_demand":
            return False
        was = self.asleep(profile)
        with self.lock:
            d = self.p[profile]
            d["wake_until"] = max(d["wake_until"] or 0, int((time.time() + 3600 * float(hours or self.wake_hours)) * 1000))
            self._save(profile)
        if was and self.on_wake:
            try:
                self.on_wake(profile)
            except Exception:
                pass
        return was

    def listing(self, profiles):
        now = time.time() * 1000
        return [{"profile": p, "priority": self.priority(p), "asleep": self.asleep(p),
                 "awake_minutes_left": max(0, round(((self.p.get(p) or {}).get("wake_until", 0) - now) / 60000))
                 if self.priority(p) == "on_demand" else None}
                for p in sorted(profiles, key=lambda x: (self.rank(x), x))]

    # ---- dashboard-picked settings (meta table)
    def setting(self, k, default=None):
        with self.store.lock:
            r = self.store.db.execute("SELECT v FROM meta WHERE k = ?", ("set:" + k,)).fetchone()
        return json.loads(r[0]) if r else default

    def set_setting(self, k, v):
        with self.store.lock:
            self.store.db.execute("INSERT OR REPLACE INTO meta VALUES (?, ?)", ("set:" + k, json.dumps(v)))
            self.store.db.commit()


# ============================================================================ local model picker
class ModelPicker:
    """List the models Ollama has, switch the one the dashboard uses (no restart), download new ones."""

    def __init__(self, llm, prefs, health=None):
        self.llm, self.prefs, self.health = llm, prefs, health
        self.pulls = {}                        # name -> {status, pct, done, error}
        saved = prefs.setting("llm_model")
        if saved:
            llm.model = saved
        ctx = prefs.setting("llm_num_ctx")
        if ctx:
            llm.num_ctx = int(ctx)
        self.source = "dashboard" if saved else "config.json"

    def installed(self):
        try:
            with urllib.request.urlopen(self.llm.url + "/api/tags", timeout=5) as r:
                ms = json.loads(r.read()).get("models", [])
        except Exception as ex:
            return None, str(ex)[:200]
        return [{"name": m.get("name"), "size_gb": round((m.get("size") or 0) / 1e9, 1),
                 "params": (m.get("details") or {}).get("parameter_size"),
                 "quant": (m.get("details") or {}).get("quantization_level")} for m in ms], None

    def status(self):
        inst, err = self.installed()
        return {"current": self.llm.model, "num_ctx": self.llm.num_ctx, "source": self.source,
                "installed": inst or [], "error": err, "pulls": self.pulls,
                "contexts": [4096, 8192, 12288, 16384, 24576, 32768]}

    def _matches(self, name, inst):
        names = {m["name"] for m in inst}
        if name in names:
            return name
        if name + ":latest" in names:
            return name + ":latest"
        return None

    def switch(self, name, num_ctx=None):
        inst, err = self.installed()
        if inst is None:
            return {"error": f"Ollama not reachable: {err}"}
        real = self._matches(name, inst)
        if not real:
            return {"error": f"{name} isn't installed. Download it first (pull), installed: "
                             + ", ".join(m["name"] for m in inst)}
        self.llm.model = real
        if num_ctx:
            self.llm.num_ctx = int(num_ctx)
        self.prefs.set_setting("llm_model", real)
        self.prefs.set_setting("llm_num_ctx", self.llm.num_ctx)
        self.source = "dashboard"
        self.llm.check(force=True)
        if self.health:
            self.health.event("info", "settings", "model",
                              f"model={real} num_ctx={self.llm.num_ctx} parallel={self.llm.gate.slots} "
                              f"max_input_chars={self.llm.max_input_chars} (switched in dashboard)")
        threading.Thread(target=self._preload, daemon=True).start()
        return {"ok": True, "model": real, "num_ctx": self.llm.num_ctx,
                "message": f"Switched to {real} (context {self.llm.num_ctx}). Loading it on the GPU now (10-30 s)."}

    def _preload(self):
        body = {"model": self.llm.model, "prompt": "ok", "stream": False, "keep_alive": "24h",
                "options": {"num_ctx": self.llm.num_ctx, "num_predict": 1}}
        try:
            req = urllib.request.Request(self.llm.url + "/api/generate", data=json.dumps(body).encode(),
                                         headers={"Content-Type": "application/json"})
            urllib.request.urlopen(req, timeout=600).read()
        except Exception as ex:
            if self.health:
                self.health.event("warning", "local_model", "preload", f"{self.llm.model}: {ex}")

    def pull(self, name):
        name = (name or "").strip()
        if not re.match(r"^[\w.\-/]+(:[\w.\-]+)?$", name):
            return {"error": "Give a model name like qwen3:30b-a3b"}
        if (self.pulls.get(name) or {}).get("done") is False:
            return {"ok": True, "message": f"{name} is already downloading."}
        self.pulls[name] = {"status": "starting", "pct": 0, "done": False, "error": None}

        def go():
            p = self.pulls[name]
            try:
                req = urllib.request.Request(self.llm.url + "/api/pull",
                                             data=json.dumps({"model": name, "stream": True}).encode(),
                                             headers={"Content-Type": "application/json"})
                with urllib.request.urlopen(req, timeout=7200) as r:
                    for line in r:
                        try:
                            d = json.loads(line)
                        except ValueError:
                            continue
                        if d.get("error"):
                            raise RuntimeError(d["error"])
                        p["status"] = d.get("status", p["status"])
                        if d.get("total"):
                            p["pct"] = round(100 * (d.get("completed") or 0) / d["total"])
                p.update(done=True, status="success", pct=100)
                if self.health:
                    self.health.event("info", "local_model", "pull", f"downloaded {name}")
            except Exception as ex:
                p.update(done=True, error=str(ex)[:300], status="failed")
                if self.health:
                    self.health.event("error", "local_model", "pull", f"{name}: {ex}")
        threading.Thread(target=go, daemon=True).start()
        return {"ok": True, "message": f"Downloading {name} in the background - progress in the Health tab."}
