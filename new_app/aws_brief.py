"""
Morning brief: once a day the tower's model (cloud when on) writes a short status of every client from the
dashboard's own data, so "how are my clients?" costs Claude ~400 tokens instead of several tool calls.
Shown at the top of the Clients tab; Claude reads it with morning_brief(). Kept in tasks.db (never cleaned).
"""
import datetime as dt
import threading
import time

# ------------------------------------------------------------------ settings
BRIEF_AT = "10:20"           # tower local time (after the daily intake at 10:00)
MAX_PATTERNS = 5             # new / spiking / regressed patterns listed per red or amber client
CONTEXT_CHARS = 60000        # snapshot size sent to the model
MODEL_KIND = "brief"         # cloud model; add "brief" to cloud_llm.local_kinds to keep it on the GPU


class Brief(threading.Thread):
    def __init__(self, llm, tasks, gather, system, max_tokens, log=print):
        super().__init__(daemon=True)
        self.llm, self.tasks, self.gather, self.system, self.max_tokens, self.log = llm, tasks, gather, system, max_tokens, log
        self.busy = False
        with tasks.lock:
            tasks.db.execute("CREATE TABLE IF NOT EXISTS briefs(day TEXT PRIMARY KEY, at INTEGER, text TEXT, ms INTEGER, error TEXT)")
            tasks.db.commit()

    def latest(self):
        with self.tasks.lock:
            r = self.tasks.db.execute("SELECT day, at, text, ms, error FROM briefs ORDER BY at DESC LIMIT 1").fetchone()
        d = dict(zip(("day", "at", "text", "ms", "error"), r)) if r else {}
        d["busy"] = self.busy
        return d

    def make(self):
        """Write today's brief now (replaces an earlier one from today)."""
        if self.busy:
            return
        self.busy = True
        t0, text, err = time.time(), None, None
        try:
            snap = self.gather()[:CONTEXT_CHARS]
            text = self.llm.chat(self.system, snap, max_tokens=self.max_tokens, kind=MODEL_KIND).strip()
        except Exception as ex:
            err = f"{type(ex).__name__}: {ex}"[:500]
            self.log("morning brief failed: " + err)
        finally:
            with self.tasks.lock:
                self.tasks.db.execute("INSERT OR REPLACE INTO briefs VALUES (?,?,?,?,?)",
                                      (dt.date.today().isoformat(), int(time.time() * 1000), text,
                                       int((time.time() - t0) * 1000), err))
                self.tasks.db.commit()
            self.busy = False

    def run(self):
        time.sleep(120)                     # let the dashboard load its data first
        while True:
            try:
                now = dt.datetime.now()
                hh, mm = (int(x) for x in BRIEF_AT.split(":"))
                last = self.latest()
                retry = last.get("error") and time.time() * 1000 - (last.get("at") or 0) > 3_600_000   # failed: hourly
                if (now.hour, now.minute) >= (hh, mm) and (last.get("day") != now.date().isoformat() or retry):
                    self.make()
            except Exception as ex:
                self.log(f"morning brief loop: {type(ex).__name__}: {ex}")
            time.sleep(60)
