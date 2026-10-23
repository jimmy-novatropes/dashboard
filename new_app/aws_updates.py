"""
Weekly client updates: once a week the tower's model drafts a short, client-friendly update email per client
from that client's 7-day health report, and adds it to the Tasks tab as a proposed task
"Send weekly update to <client>" with the draft in the notes - edit and send it yourself.
"""
import datetime as dt
import threading
import time

# ------------------------------------------------------------------ settings
WEEKDAY = 4                  # 0 = Monday ... 4 = Friday
SEND_AT = "15:00"            # tower local time
CLIENTS = "high"             # "high" = starred clients only; "all" = every client not on demand
MODEL_KIND = "update"        # cloud model; add "update" to cloud_llm.local_kinds to keep it on the GPU

SYSTEM = """Write a short weekly status email from Open Flow (the integration team) to a client, based on this
7-day health report of their integrations. Friendly, plain language, no internal jargon, no blame, no
log lines or code. Structure: greeting; 2-4 sentences on how the week went (syncs running, volumes);
what went wrong and what was done or is planned (only what the report shows); anything we need from
them; sign-off "Best, Jimmy". Max 180 words. Start with a line "Subject: ..."."""


class WeeklyUpdates(threading.Thread):
    def __init__(self, llm, tasks, report_fn, clients_fn, log=print):
        super().__init__(daemon=True)
        self.llm, self.tasks, self.report_fn, self.clients_fn, self.log = llm, tasks, report_fn, clients_fn, log
        self.busy = False
        with tasks.lock:
            tasks.db.execute("CREATE TABLE IF NOT EXISTS update_runs(week TEXT PRIMARY KEY, at INTEGER, drafted INTEGER, error TEXT)")
            tasks.db.commit()

    @staticmethod
    def week():
        y, w, _ = dt.date.today().isocalendar()
        return f"{y}-W{w:02d}"

    def last(self):
        with self.tasks.lock:
            r = self.tasks.db.execute("SELECT week, at, drafted, error FROM update_runs ORDER BY at DESC LIMIT 1").fetchone()
        return dict(zip(("week", "at", "drafted", "error"), r)) if r else {}

    def make(self):
        if self.busy:
            return
        self.busy = True
        wk, n, errs = self.week(), 0, []
        try:
            for prof in self.clients_fn(CLIENTS):
                try:
                    md = self.report_fn(prof)
                    draft = self.llm.chat(SYSTEM, f"Client: {prof}\n\n{md[:40000]}", max_tokens=700, kind=MODEL_KIND).strip()
                    r = self.tasks.add([{"title": f"Send weekly update to {prof}", "client": prof, "source": "report",
                                         "status": "proposed", "external_id": f"weekly:{prof}:{wk}",
                                         "request": f"Weekly status email for {prof} ({wk}) - draft in the notes.",
                                         "reason": "Weekly client update", "notes": "Draft (edit before sending):\n" + draft}])
                    n += len(r.get("added", []))
                except Exception as ex:
                    errs.append(f"{prof}: {type(ex).__name__}: {ex}"[:200])
            self.log(f"weekly updates {wk}: {n} draft(s)" + (f"; problems: {'; '.join(errs)}" if errs else ""))
        finally:
            with self.tasks.lock:
                self.tasks.db.execute("INSERT INTO update_runs VALUES (?,?,?,?) ON CONFLICT(week) DO UPDATE SET "
                                      "at = excluded.at, drafted = drafted + excluded.drafted, error = excluded.error",
                                      (wk, int(time.time() * 1000), n, "; ".join(errs)[:1000] or None))
                self.tasks.db.commit()
            self.busy = False

    def run(self):
        time.sleep(150)
        while True:
            try:
                now = dt.datetime.now()
                hh, mm = (int(x) for x in SEND_AT.split(":"))
                if now.weekday() == WEEKDAY and (now.hour, now.minute) >= (hh, mm) and self.last().get("week") != self.week():
                    self.make()
            except Exception as ex:
                self.log(f"weekly updates loop: {type(ex).__name__}: {ex}")
            time.sleep(300)
