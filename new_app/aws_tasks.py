"""
Tasks / follow-ups for the AWS Error Feed, kept in their own SQLite file (tasks.db, next to this
script). errors.db is a 24h cache that gets slimmed and reset; tasks.db is never cleaned up.

What goes in: things you need to do - from emails (Claude reads your mail in a chat and adds them with
the tasks_add tool), from error patterns ("+ task" in the dashboard) or added by hand.
From an email only the request, the reason and any links are kept - never the email body.

Statuses: open, in_progress, waiting, done; proposed (added by the daily intake, waiting for you to accept)
and dismissed (a proposed task you said no to - kept so it isn't proposed again).   Priorities: high, normal, low.
Duplicates: an item with the same external_id (e.g. the Gmail thread id), or an open item with the same
title + client, is updated instead of added twice.
"""
import json
import os
import re
import sqlite3
import threading
import time

STATUSES = ("open", "in_progress", "waiting", "done", "proposed", "dismissed")
HIDDEN = ("proposed", "dismissed")      # not in the normal list / counts until accepted
PRIORITIES = ("high", "normal", "low")
SOURCES = ("email", "error", "meeting", "manual")
FIELDS = ("title", "request", "reason", "links", "client", "requester", "source", "status", "priority",
          "due", "external_id", "error_ref", "notes")
_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_URL = re.compile(r"https?://[^\s<>\"')\]]+")


def _now():
    return int(time.time() * 1000)


def _norm(s):
    return re.sub(r"\s+", " ", str(s or "")).strip()


def _links(v):
    """Accept a list or a text with URLs -> de-duplicated list of URLs."""
    if isinstance(v, str):
        v = _URL.findall(v) or ([v] if v.strip().startswith("http") else [])
    out = []
    for x in v or []:
        x = _norm(x).rstrip(".,;")
        if x.lower().startswith(("http://", "https://")) and x not in out:
            out.append(x[:1000])
    return out[:20]


class TaskDB:
    def __init__(self, path):
        self.path = path
        self.lock = threading.Lock()
        self.db = sqlite3.connect(path, check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.executescript("""
            PRAGMA journal_mode=WAL;
            CREATE TABLE IF NOT EXISTS items(
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                title TEXT NOT NULL, request TEXT, reason TEXT, links TEXT,
                client TEXT, requester TEXT, source TEXT, status TEXT, priority TEXT, due TEXT,
                external_id TEXT, error_ref TEXT, notes TEXT,
                created INTEGER, updated INTEGER, done_at INTEGER);
            CREATE INDEX IF NOT EXISTS items_status ON items(status);
            CREATE UNIQUE INDEX IF NOT EXISTS items_ext ON items(external_id) WHERE external_id IS NOT NULL;
            CREATE TABLE IF NOT EXISTS item_log(item_id INTEGER, at INTEGER, change TEXT);
        """)
        self.db.commit()

    # ------------------------------------------------------------------ helpers
    @staticmethod
    def _row(r):
        d = dict(r)
        try:
            d["links"] = json.loads(d.get("links") or "[]")
        except ValueError:
            d["links"] = []
        return d

    def _clean(self, it, partial=False):
        """Validate / normalise one item's fields. partial=True: only the keys given."""
        out = {}
        for k in FIELDS:
            if k not in it:
                continue
            v = it[k]
            if k == "links":
                out[k] = json.dumps(_links(v))
                continue
            v = _norm(v) if k not in ("request", "reason", "notes") else str(v or "").strip()
            if k == "status":
                v = v.lower().replace(" ", "_").replace("-", "_") or "open"
                if v not in STATUSES:
                    raise ValueError(f"status must be one of {STATUSES}")
            elif k == "priority":
                v = v.lower() or "normal"
                if v not in PRIORITIES:
                    raise ValueError(f"priority must be one of {PRIORITIES}")
            elif k == "source":
                v = v.lower() or "manual"
                if v not in SOURCES:
                    v = "manual"
            elif k == "due" and v and not _DATE.match(v):
                raise ValueError("due must be YYYY-MM-DD")
            elif k == "title":
                if not v and partial:
                    raise ValueError("title can't be empty")
                v = v[:300]
            elif k in ("request", "reason", "notes"):
                v = v[:4000]
            out[k] = v or None
        if not partial:
            if not out.get("title"):
                req = _norm(it.get("request"))
                if not req:
                    raise ValueError("each item needs a title (or at least a request)")
                out["title"] = req[:120]
            out.setdefault("status", "open")
            out.setdefault("priority", "normal")
            out.setdefault("source", "manual")
        return out

    def _log(self, item_id, change):
        self.db.execute("INSERT INTO item_log VALUES (?,?,?)", (item_id, _now(), change[:500]))

    def _find_dupe(self, f):
        if f.get("external_id"):
            r = self.db.execute("SELECT * FROM items WHERE external_id = ?", (f["external_id"],)).fetchone()
            if r:
                return r
        return self.db.execute(
            "SELECT * FROM items WHERE status != 'done' AND lower(title) = lower(?) AND ifnull(client,'') = ?",
            (f["title"], f.get("client") or "")).fetchone()

    # ------------------------------------------------------------------ write
    def add(self, items):
        """Add or update a batch. -> {"added": [ids], "updated": [ids], "errors": [...]}"""
        res = {"added": [], "updated": [], "unchanged": [], "errors": []}
        with self.lock:
            for i, it in enumerate(items or []):
                try:
                    f = self._clean(it if isinstance(it, dict) else {"title": str(it)})
                except ValueError as ex:
                    res["errors"].append(f"item {i + 1}: {ex}")
                    continue
                old = self._find_dupe(f)
                if old:
                    # an existing item keeps its status / priority / due; new text and links are merged in
                    ch = {k: v for k, v in f.items()
                          if k in ("request", "reason", "requester", "external_id", "error_ref") and v and v != old[k]}
                    links = _links(json.loads(old["links"] or "[]") + json.loads(f.get("links") or "[]"))
                    if links != json.loads(old["links"] or "[]"):
                        ch["links"] = json.dumps(links)
                    if ch:
                        ch["updated"] = _now()
                        self.db.execute(f"UPDATE items SET {', '.join(k + ' = ?' for k in ch)} WHERE id = ?",
                                        (*ch.values(), old["id"]))
                        self._log(old["id"], "updated from new input: " + ", ".join(k for k in ch if k != "updated"))
                        res["updated"].append(old["id"])
                    else:
                        res["unchanged"].append(old["id"])
                    continue
                now = _now()
                f.update(created=now, updated=now, done_at=now if f["status"] == "done" else None)
                cur = self.db.execute(f"INSERT INTO items({', '.join(f)}) VALUES ({', '.join('?' * len(f))})",
                                      tuple(f.values()))
                self._log(cur.lastrowid, f"added ({f['source']})")
                res["added"].append(cur.lastrowid)
            self.db.commit()
        return res

    def update(self, item_id, fields):
        f = self._clean(fields, partial=True)
        if not f:
            raise ValueError("nothing to change")
        with self.lock:
            old = self.db.execute("SELECT * FROM items WHERE id = ?", (int(item_id),)).fetchone()
            if not old:
                raise ValueError(f"no item #{item_id}")
            if "status" in f and f["status"] != old["status"]:
                f["done_at"] = _now() if f["status"] == "done" else None
            f["updated"] = _now()
            self.db.execute(f"UPDATE items SET {', '.join(k + ' = ?' for k in f)} WHERE id = ?",
                            (*f.values(), int(item_id)))
            self._log(int(item_id), "; ".join(f"{k}: {old[k]!s:.40} -> {f[k]!s:.60}" for k in f
                                              if k not in ("updated", "done_at") and old[k] != f[k]) or "touched")
            self.db.commit()
        return self.get(item_id)

    def delete(self, item_id):
        with self.lock:
            n = self.db.execute("DELETE FROM items WHERE id = ?", (int(item_id),)).rowcount
            self._log(int(item_id), "deleted")
            self.db.commit()
        return n

    # ------------------------------------------------------------------ read
    def get(self, item_id):
        with self.lock:
            r = self.db.execute("SELECT * FROM items WHERE id = ?", (int(item_id),)).fetchone()
            log = self.db.execute("SELECT at, change FROM item_log WHERE item_id = ? ORDER BY at",
                                  (int(item_id),)).fetchall() if r else []
        if not r:
            return None
        d = self._row(r)
        d["history"] = [dict(x) for x in log]
        return d

    def list(self, status="", client="", q="", done_days=7, limit=500):
        """status: '' = everything not done + items done in the last done_days; 'all'; or a comma list."""
        sql, args = "SELECT * FROM items WHERE 1=1", []
        if status in ("", None):
            sql += " AND status NOT IN ('proposed','dismissed') AND (status != 'done' OR done_at >= ?)"
            args.append(_now() - int(float(done_days or 0) * 86_400_000))
        elif status != "all":
            st = [s.strip() for s in status.split(",") if s.strip()]
            sql += f" AND status IN ({','.join('?' * len(st))})"
            args += st
        if client:
            sql += " AND lower(ifnull(client,'')) = lower(?)"
            args.append(client)
        if q:
            sql += " AND (title LIKE ? OR request LIKE ? OR reason LIKE ? OR requester LIKE ? OR notes LIKE ?)"
            args += [f"%{q}%"] * 5
        sql += (" ORDER BY CASE status WHEN 'done' THEN 1 ELSE 0 END, CASE priority WHEN 'high' THEN 0 "
                "WHEN 'normal' THEN 1 ELSE 2 END, CASE WHEN due IS NULL THEN 1 ELSE 0 END, due, updated DESC LIMIT ?")
        args.append(int(limit))
        with self.lock:
            return [self._row(r) for r in self.db.execute(sql, args).fetchall()]

    def counts(self):
        """Open (not done) items per client + overdue per client."""
        today = time.strftime("%Y-%m-%d")
        with self.lock:
            rows = self.db.execute(
                "SELECT ifnull(client,''), count(*), sum(CASE WHEN due IS NOT NULL AND due < ? THEN 1 ELSE 0 END) "
                "FROM items WHERE status NOT IN ('done','proposed','dismissed') GROUP BY 1", (today,)).fetchall()
        return {r[0]: {"open": r[1], "overdue": r[2] or 0} for r in rows}

    def clients(self):
        with self.lock:
            return [r[0] for r in self.db.execute(
                "SELECT DISTINCT client FROM items WHERE client IS NOT NULL ORDER BY 1").fetchall()]


def open_db(here, cfg):
    p = cfg.get("tasks_db") or "tasks.db"
    return TaskDB(p if os.path.isabs(p) else os.path.join(here, p))
