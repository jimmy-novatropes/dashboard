"""
Personal tasks: a to-do list kept apart from the work tasks, in its own SQLite file (personal.db, next to this
script, git-ignored, never synced). Claude only adds / lists them; the tower's Ollama model does the research in
the background and fills in "What to do next" for each task.

Researcher (background thread):
  1. picks the oldest task waiting for research (new, edited, or "research again")
  2. the cloud model (Ollama, gpt-oss) decides whether the web helps and plans search queries
  3. Ollama web search + web fetch read the pages (same API key as aws_research)
  4. the model writes "What to do next" (short numbered steps) + findings with [n] citations
Open tasks are researched again every REFRESH_DAYS days, so prices / dates / availability stay current.
"""
import json
import os
import re
import sqlite3
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import aws_research

# ------------------------------------------------------------------ settings
POLL_S = 30                    # how often the researcher looks for work
REFRESH_DAYS = 7               # open tasks are researched again after this many days (0 = never)
DEPTH = "normal"               # quick | normal | deep (queries / pages: see aws_research.DEPTH)
MODEL_KIND = "personal"        # LLM routing kind: cloud model, background (falls back to local above the cap)
REPORT_TOKENS = 1800
MAX_TRIES = 3                  # research errors before a task is left as "error"

STATUSES = ("open", "in_progress", "waiting", "done")
PRIORITIES = ("high", "normal", "low")
R_STATES = ("queued", "running", "done", "error", "off")     # research_status
_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")

PLAN_SYSTEM = """You help with someone's personal to-do list. Given one task, decide if web research would help
(prices, options, how-tos, rules, opening hours, deadlines, booking, comparisons, local services) and plan it.
Reply JSON only: {"needs_web": true|false, "queries": ["...", "..."]} with up to 6 short, specific search-engine
queries covering different angles. needs_web=false for tasks that are just reminders (call someone, pay a known
bill) - then queries=[]. Include the year when recency matters."""

NEXT_SYSTEM = """You help someone get a personal task done. Using the numbered sources (if any) and common sense,
reply in exactly this format:

NEXT STEPS:
1. <concrete action they can do now - who / where / how, with a [n] citation when a source supports it>
2. ...
(3-6 steps, most important first; include costs, deadlines, links or phone numbers when the sources give them)

FINDINGS:
<the key facts / options found, compact, every claim cited with [n]; say what is unclear, old or vendor marketing>

Be specific and practical. No filler. Max 350 words total."""


def _now():
    return int(time.time() * 1000)


def _norm(s):
    return re.sub(r"\s+", " ", str(s or "")).strip()


class PersonalDB:
    FIELDS = ("title", "details", "category", "status", "priority", "due", "notes")

    def __init__(self, path):
        self.path = path
        self.lock = threading.Lock()
        self.db = sqlite3.connect(path, check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.executescript("""
            PRAGMA journal_mode=WAL;
            CREATE TABLE IF NOT EXISTS personal(
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                title TEXT NOT NULL, details TEXT, category TEXT, status TEXT, priority TEXT, due TEXT, notes TEXT,
                next_steps TEXT, findings TEXT, sources TEXT, queries TEXT,
                research_status TEXT, research_error TEXT, research_tries INTEGER DEFAULT 0,
                researched_at INTEGER, research_model TEXT,
                created INTEGER, updated INTEGER, done_at INTEGER);
            CREATE INDEX IF NOT EXISTS personal_status ON personal(status);
            CREATE TABLE IF NOT EXISTS personal_log(item_id INTEGER, at INTEGER, change TEXT);
        """)
        self.db.commit()

    @staticmethod
    def _row(r):
        d = dict(r)
        for k in ("sources", "queries"):
            try:
                d[k] = json.loads(d.get(k) or "[]")
            except ValueError:
                d[k] = []
        return d

    def _clean(self, it, partial=False):
        out = {}
        for k in self.FIELDS:
            if k not in it:
                continue
            v = str(it[k] or "").strip() if k in ("details", "notes") else _norm(it[k])
            if k == "status":
                v = v.lower().replace(" ", "_").replace("-", "_") or "open"
                if v not in STATUSES:
                    raise ValueError(f"status must be one of {STATUSES}")
            elif k == "priority":
                v = v.lower() or "normal"
                if v not in PRIORITIES:
                    raise ValueError(f"priority must be one of {PRIORITIES}")
            elif k == "due" and v and not _DATE.match(v):
                raise ValueError("due must be YYYY-MM-DD")
            elif k == "title":
                if not v:
                    raise ValueError("title can't be empty")
                v = v[:300]
            out[k] = (v[:4000] if v else None)
        if not partial:
            if not out.get("title"):
                raise ValueError("each task needs a title")
            out.setdefault("status", "open")
            out.setdefault("priority", "normal")
        return out

    def _log(self, item_id, change):
        self.db.execute("INSERT INTO personal_log VALUES (?,?,?)", (item_id, _now(), change[:500]))

    # ------------------------------------------------------------------ write
    def add(self, items):
        res = {"added": [], "updated": [], "errors": []}
        with self.lock:
            for i, it in enumerate(items or []):
                try:
                    it = it if isinstance(it, dict) else {"title": str(it)}
                    f = self._clean(it)
                except ValueError as ex:
                    res["errors"].append(f"item {i + 1}: {ex}")
                    continue
                old = self.db.execute("SELECT * FROM personal WHERE status != 'done' AND lower(title) = lower(?)",
                                      (f["title"],)).fetchone()
                if old:
                    if f.get("details") and f["details"] != old["details"]:
                        self.db.execute("UPDATE personal SET details = ?, updated = ?, research_status = 'queued', "
                                        "research_tries = 0 WHERE id = ?", (f["details"], _now(), old["id"]))
                        self._log(old["id"], "details updated -> research again")
                    res["updated"].append(old["id"])
                    continue
                now = _now()
                f.update(created=now, updated=now, done_at=now if f["status"] == "done" else None,
                         research_status="off" if it.get("research") is False else "queued")
                cur = self.db.execute(f"INSERT INTO personal({', '.join(f)}) VALUES ({', '.join('?' * len(f))})",
                                      tuple(f.values()))
                self._log(cur.lastrowid, "added")
                res["added"].append(cur.lastrowid)
            self.db.commit()
        return res

    def update(self, item_id, fields):
        research = fields.pop("research", None)
        f = self._clean(fields, partial=True)
        with self.lock:
            old = self.db.execute("SELECT * FROM personal WHERE id = ?", (int(item_id),)).fetchone()
            if not old:
                raise ValueError(f"no personal task #{item_id}")
            if "status" in f and f["status"] != old["status"]:
                f["done_at"] = _now() if f["status"] == "done" else None
            if research is True or (("title" in f and f["title"] != old["title"]) or
                                    ("details" in f and f["details"] != old["details"])):
                f.update(research_status="queued", research_tries=0)
            elif research is False:
                f["research_status"] = "off"
            if not f:
                raise ValueError("nothing to change")
            f["updated"] = _now()
            self.db.execute(f"UPDATE personal SET {', '.join(k + ' = ?' for k in f)} WHERE id = ?",
                            (*f.values(), int(item_id)))
            self._log(int(item_id), "; ".join(f"{k}: {old[k]!s:.40} -> {f[k]!s:.60}" for k in f
                                              if k not in ("updated", "done_at") and old[k] != f[k]) or "touched")
            self.db.commit()
        return self.get(item_id)

    def delete(self, item_id):
        with self.lock:
            n = self.db.execute("DELETE FROM personal WHERE id = ?", (int(item_id),)).rowcount
            self._log(int(item_id), "deleted")
            self.db.commit()
        return n

    def set_research(self, item_id, **f):
        with self.lock:
            self.db.execute(f"UPDATE personal SET {', '.join(k + ' = ?' for k in f)} WHERE id = ?",
                            (*f.values(), int(item_id)))
            self.db.commit()

    # ------------------------------------------------------------------ read
    def get(self, item_id):
        with self.lock:
            r = self.db.execute("SELECT * FROM personal WHERE id = ?", (int(item_id),)).fetchone()
            log = self.db.execute("SELECT at, change FROM personal_log WHERE item_id = ? ORDER BY at",
                                  (int(item_id),)).fetchall() if r else []
        if not r:
            return None
        d = self._row(r)
        d["history"] = [dict(x) for x in log]
        return d

    def list(self, status="", q="", done_days=7, limit=500):
        sql, args = "SELECT * FROM personal WHERE 1=1", []
        if status in ("", None):
            sql += " AND (status != 'done' OR done_at >= ?)"
            args.append(_now() - int(float(done_days or 0) * 86_400_000))
        elif status != "all":
            st = [s.strip() for s in status.split(",") if s.strip()]
            sql += f" AND status IN ({','.join('?' * len(st))})"
            args += st
        if q:
            sql += " AND (title LIKE ? OR details LIKE ? OR notes LIKE ? OR next_steps LIKE ? OR category LIKE ?)"
            args += [f"%{q}%"] * 5
        sql += (" ORDER BY CASE status WHEN 'done' THEN 1 ELSE 0 END, CASE priority WHEN 'high' THEN 0 "
                "WHEN 'normal' THEN 1 ELSE 2 END, CASE WHEN due IS NULL THEN 1 ELSE 0 END, due, updated DESC LIMIT ?")
        args.append(int(limit))
        with self.lock:
            return [self._row(r) for r in self.db.execute(sql, args).fetchall()]

    def counts(self):
        today = time.strftime("%Y-%m-%d")
        with self.lock:
            r = self.db.execute(
                "SELECT count(*), sum(CASE WHEN due IS NOT NULL AND due < ? THEN 1 ELSE 0 END), "
                "sum(CASE WHEN research_status IN ('queued','running') THEN 1 ELSE 0 END) "
                "FROM personal WHERE status != 'done'", (today,)).fetchone()
        return {"open": r[0] or 0, "overdue": r[1] or 0, "researching": r[2] or 0}

    def next_for_research(self):
        """Oldest queued open task; else an open task whose research is older than REFRESH_DAYS."""
        with self.lock:
            # a task left "running" by a restart goes back in the queue
            self.db.execute("UPDATE personal SET research_status = 'queued' WHERE research_status = 'running'")
            r = self.db.execute("SELECT * FROM personal WHERE status != 'done' AND research_status = 'queued' "
                                "ORDER BY CASE priority WHEN 'high' THEN 0 WHEN 'normal' THEN 1 ELSE 2 END, created "
                                "LIMIT 1").fetchone()
            if not r and REFRESH_DAYS:
                r = self.db.execute("SELECT * FROM personal WHERE status != 'done' AND research_status = 'done' "
                                    "AND researched_at < ? ORDER BY researched_at LIMIT 1",
                                    (_now() - REFRESH_DAYS * 86_400_000,)).fetchone()
            return self._row(r) if r else None


# ============================================================================ researcher
class Researcher:
    def __init__(self, db, llm, key_fn, event=lambda *a: None):
        self.db, self.llm, self.key_fn, self.event = db, llm, key_fn, event
        self.state = {"busy": False, "current": None, "stage": "", "done": 0, "errors": 0, "last_error": ""}
        self.wake = threading.Event()

    def start(self):
        threading.Thread(target=self.loop, daemon=True, name="personal-research").start()

    def status(self):
        return {**self.state, "has_key": bool(self.key_fn()), "refresh_days": REFRESH_DAYS}

    def loop(self):
        time.sleep(20)                                  # let the dashboard finish starting
        while True:
            try:
                it = self.db.next_for_research() if self.llm.ready else None
                if it:
                    self.run(it)
                    continue
            except Exception as ex:                     # never let the loop die
                self.state["last_error"] = f"{type(ex).__name__}: {ex}"[:300]
            self.wake.wait(POLL_S)
            self.wake.clear()

    def _stage(self, s):
        self.state["stage"] = s

    def run(self, it):
        self.state.update(busy=True, current=it["id"], stage="planning")
        self.db.set_research(it["id"], research_status="running")
        try:
            next_steps, findings, sources, queries = self.research(it)
            self.db.set_research(it["id"], research_status="done", next_steps=next_steps, findings=findings,
                                 sources=json.dumps(sources), queries=json.dumps(queries), research_error=None,
                                 research_tries=0, researched_at=_now(), research_model=self.llm.model_for(MODEL_KIND))
            self.state["done"] += 1
        except Exception as ex:
            err = f"{type(ex).__name__}: {ex}"[:300]
            tries = (it.get("research_tries") or 0) + 1
            self.db.set_research(it["id"], research_status="error" if tries >= MAX_TRIES else "queued",
                                 research_error=err, research_tries=tries)
            self.state.update(errors=self.state["errors"] + 1, last_error=err)
            self.event("warning", "personal", "research", f"#{it['id']}: {err}")
            if tries < MAX_TRIES:
                time.sleep(60)                          # don't hammer a failing API
        finally:
            self.state.update(busy=False, current=None, stage="")

    def research(self, it):
        today = time.strftime("%Y-%m-%d")
        task = (f"Task: {it['title']}" + (f"\nDetails: {it['details']}" if it.get("details") else "")
                + (f"\nDue: {it['due']}" if it.get("due") else "") + (f"\nNotes: {it['notes']}" if it.get("notes") else ""))
        key = self.key_fn()
        queries, pages = [], []
        if key:
            try:
                plan = self.llm.chat(PLAN_SYSTEM, f"Today: {today}\n{task}", as_json=True, max_tokens=400, kind=MODEL_KIND)
                if not isinstance(plan, dict):
                    plan = json.loads(re.search(r"\{.*\}", str(plan), re.S).group(0))
                n_q, n_pages = aws_research.DEPTH.get(DEPTH, aws_research.DEPTH["normal"])
                queries = [str(q) for q in plan.get("queries") or []][:n_q] if plan.get("needs_web", True) else []
            except Exception:
                n_q, n_pages = aws_research.DEPTH["normal"]
                queries = [it["title"][:200]]
            if queries:
                self._stage(f"searching ({len(queries)} queries)")
                per_q = []
                for q in queries:
                    try:
                        per_q.append(aws_research.web_search(q, key))
                    except Exception:
                        per_q.append([])
                seen, picks = set(), []
                for i in range(aws_research.RESULTS_PER_QUERY):
                    for res in per_q:
                        if i < len(res) and res[i].get("url") and res[i]["url"] not in seen:
                            seen.add(res[i]["url"])
                            picks.append(res[i])
                picks = picks[:n_pages]
                self._stage(f"reading {len(picks)} pages")

                def read(p):
                    try:
                        d = aws_research.web_fetch(p["url"], key)
                        txt = d.get("content") or p.get("content") or ""
                        return {"title": d.get("title") or p.get("title") or p["url"], "url": p["url"],
                                "text": txt[:aws_research.PAGE_CHARS]}
                    except Exception:
                        return {"title": p.get("title") or p["url"], "url": p["url"],
                                "text": (p.get("content") or "")[:aws_research.PAGE_CHARS]}
                with ThreadPoolExecutor(4) as ex:
                    pages = list(ex.map(read, picks))
        self._stage("writing next steps")
        src = "\n\n".join(f"[{i}] {p['title']} - {p['url']}\n{p['text']}" for i, p in enumerate(pages, 1))
        ans = self.llm.chat(NEXT_SYSTEM, f"Today: {today}\n{task}\n\nSources:\n{src or '(none - use common sense)'}",
                            max_tokens=REPORT_TOKENS, kind=MODEL_KIND).strip()
        m = re.search(r"NEXT STEPS:\s*(.*?)(?:\n\s*FINDINGS:\s*(.*))?$", ans, re.S | re.I)
        next_steps = (m.group(1).strip() if m else ans)[:3000]
        findings = ((m.group(2) or "").strip() if m else "")[:6000]
        return next_steps, findings, [{"title": p["title"][:200], "url": p["url"]} for p in pages], queries


def open_db(here, cfg):
    p = cfg.get("personal_db") or "personal.db"
    return PersonalDB(p if os.path.isabs(p) else os.path.join(here, p))
