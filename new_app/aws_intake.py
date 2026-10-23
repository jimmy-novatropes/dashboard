"""
Daily intake for the Tasks tab: every morning the dashboard reads yesterday's inbox (Gmail) and meetings
(Fireflies), lets the tower's model pull out what YOU need to do, and adds those to tasks.db as 'proposed'
(accept / dismiss on the Tasks tab). Runs inside the dashboard on the tower - Claude isn't involved.

One-time setup (see deploy_tower.py, GMAIL_SIGN_IN):
  - new_app/gmail_client.json : OAuth "Desktop app" client from Google Cloud (Gmail API enabled), git-ignored
  - new_app/gmail_token.json  : written by the one-time sign-in in the browser, git-ignored
  - config.json "intake": {"enabled": true, "owner_email": "...", "fireflies_api_key": "..."}
From an email only the request, the reason and a link are kept - never the email body.
"""
import base64
import datetime as dt
import html
import json
import os
import re
import threading
import time
import urllib.request

# ------------------------------------------------------------------ settings
RUN_AT = "10:00"                 # tower local time; if the tower was off then, it runs as soon as it's back that day
LOOKBACK_DAYS = 1                # 1 = yesterday, midnight to midnight
RETRY_MINUTES = 60               # a run with problems (Gmail / Fireflies / model) is retried this often that day
TASK_STATUS = "proposed"         # new tasks land here; accept -> open, dismiss -> dismissed
MODEL_KIND = "intake"            # goes to the cloud model; add "intake" to cloud_llm.local_kinds to keep mail on the GPU
GMAIL_QUERY = "in:inbox -category:promotions -category:social -category:updates -category:forums"
SKIP_SENDERS = ("noreply", "no-reply", "donotreply", "do-not-reply", "notifications@", "notification@",
                "mailer-daemon", "postmaster", "@calendar.google.com", "calendar-notification", "invitations@")
SKIP_BULK = True                 # skip mail with List-Unsubscribe / Precedence: bulk / Auto-Submitted
MAX_EMAILS = 150
EMAIL_CHARS = 3000               # per email, after quoted replies are cut
MEETING_CHARS = 6000             # per meeting (Fireflies action items + overview)
FIREFLIES_ONLY_MINE = True       # only meetings you organised or were invited to
BATCH_CHARS = 24000              # text per model call
MAX_TOKENS = 3500                # model answer size per batch (tasks + reply drafts)
GMAIL_CLIENT_FILE = "gmail_client.json"
GMAIL_TOKEN_FILE = "gmail_token.json"
GMAIL_SCOPES = ["https://www.googleapis.com/auth/gmail.readonly"]
GMAIL_API = "https://gmail.googleapis.com/gmail/v1/users/me"
FIREFLIES_URL = "https://api.fireflies.ai/graphql"

SYSTEM = """You turn one person's emails and meeting notes into their to-do list.
The person: {owner} at Open Flow, a consultancy that builds and runs integrations (HubSpot, NetSuite, AWS
Lambda / Step Functions syncs) for clients. Known clients: {clients}.
Include only things THIS person needs to do or follow up on: requests made to them or to Open Flow,
commitments they made ("I'll send...", "we will fix..."), meeting action items assigned to them, to
Open Flow or to "the team", and questions waiting for their answer.
Skip: FYIs, newsletters, automated alerts and receipts, things already done or answered in the same
thread, and tasks that belong to someone else.
Answer JSON only: {{"tasks": [{{"source_id": "<the [E..] / [M..] id of the item>", "title": "short imperative,
max 10 words", "request": "what is asked, 1-2 sentences", "reason": "why it matters / context, 1 sentence",
"client": "one of the known clients or empty", "requester": "Name <email> or empty", "due": "YYYY-MM-DD or
empty", "priority": "high|normal|low", "reply_draft": "for an email that needs an answer from the person: a short,
friendly reply they can edit (no promises of dates they didn't make; sign 'Best, Jimmy'); else empty"}}]}}.
One task per distinct action. No tasks -> {{"tasks": []}}."""


def _hook(*a):
    try:
        import aws_local
        aws_local._hook(*a)
    except Exception:
        pass


def day_range(day):
    """Local midnight..midnight for a date -> (start_ts, end_ts) in seconds."""
    start = dt.datetime.combine(day, dt.time())
    return int(time.mktime(start.timetuple())), int(time.mktime((start + dt.timedelta(days=1)).timetuple()))


# ------------------------------------------------------------------ Gmail
def gmail_session(here):
    from google.oauth2.credentials import Credentials
    from google.auth.transport.requests import AuthorizedSession, Request
    path = os.path.join(here, GMAIL_TOKEN_FILE)
    if not os.path.exists(path):
        raise RuntimeError(f"Gmail not signed in ({GMAIL_TOKEN_FILE} missing) - run deploy_tower.py with GMAIL_SIGN_IN = True")
    creds = Credentials.from_authorized_user_file(path, GMAIL_SCOPES)
    if not creds.valid:
        creds.refresh(Request())
        with open(path, "w") as f:
            f.write(creds.to_json())
    return AuthorizedSession(creds)


def gmail_sign_in(here):
    """One-time, on the tower: opens the browser to allow read-only Gmail access, saves gmail_token.json."""
    from google_auth_oauthlib.flow import InstalledAppFlow
    client = os.path.join(here, GMAIL_CLIENT_FILE)
    if not os.path.exists(client):
        raise RuntimeError(f"{GMAIL_CLIENT_FILE} missing in {here} - download the OAuth Desktop client JSON first")
    creds = InstalledAppFlow.from_client_secrets_file(client, GMAIL_SCOPES).run_local_server(port=0)
    with open(os.path.join(here, GMAIL_TOKEN_FILE), "w") as f:
        f.write(creds.to_json())
    return True


def _b64(s):
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4)).decode("utf-8", "replace")


def _body(payload):
    """text/plain if there is one, else text/html stripped of tags."""
    plain, htm = [], []

    def walk(p):
        mt, data = p.get("mimeType", ""), (p.get("body") or {}).get("data")
        if data and mt == "text/plain":
            plain.append(_b64(data))
        elif data and mt == "text/html":
            htm.append(_b64(data))
        for c in p.get("parts") or []:
            walk(c)
    walk(payload)
    if plain:
        return "\n".join(plain)
    t = re.sub(r"(?is)<(script|style).*?</\1>", " ", "\n".join(htm))
    t = re.sub(r"(?i)<br\s*/?>|</p>|</div>|</tr>", "\n", t)
    return html.unescape(re.sub(r"<[^>]+>", " ", t))


_QUOTE = re.compile(r"^(On .{5,200} wrote:|-----Original Message-----|From: .+|_{10,}|Le .{5,200} a écrit\s?:)\s*$")


def strip_quotes(text):
    out = []
    for line in text.splitlines():
        s = line.strip()
        if _QUOTE.match(s):
            break
        if s.startswith(">"):
            continue
        out.append(line.rstrip())
    return re.sub(r"\n{3,}", "\n\n", "\n".join(out)).strip()


def fetch_emails(sess, owner, start, end, log=print):
    q = f"{GMAIL_QUERY} after:{start} before:{end}"
    ids, token = [], None
    while len(ids) < MAX_EMAILS:
        r = sess.get(GMAIL_API + "/messages", params={"q": q, "maxResults": 100, **({"pageToken": token} if token else {})},
                     timeout=60)
        r.raise_for_status()
        d = r.json()
        ids += [m["id"] for m in d.get("messages") or []]
        token = d.get("nextPageToken")
        if not token:
            break
    out, skipped = [], 0
    for mid in ids[:MAX_EMAILS]:
        r = sess.get(f"{GMAIL_API}/messages/{mid}", params={"format": "full"}, timeout=60)
        r.raise_for_status()
        m = r.json()
        h = {x["name"].lower(): x["value"] for x in (m.get("payload") or {}).get("headers") or []}
        frm = h.get("from", "")
        if any(s in frm.lower() for s in SKIP_SENDERS) or (owner and owner.lower() in frm.lower()):
            skipped += 1
            continue
        if SKIP_BULK and (h.get("list-unsubscribe") or h.get("precedence", "").lower() in ("bulk", "list", "junk")
                          or h.get("auto-submitted", "no").lower() != "no"):
            skipped += 1
            continue
        out.append({"kind": "email", "id": "gmail:" + mid, "thread": m.get("threadId"),
                    "link": f"https://mail.google.com/mail/u/{owner or 0}/#all/{m.get('threadId')}",
                    "head": f"EMAIL from {frm} | to {h.get('to', '')[:200]}"
                            + (f" | cc {h.get('cc', '')[:150]}" if h.get("cc") else "")
                            + f" | subject: {h.get('subject', '')} | {h.get('date', '')}",
                    "text": strip_quotes(_body(m.get("payload") or {}))[:EMAIL_CHARS] or m.get("snippet", "")})
    log(f"intake: {len(ids)} emails found, {skipped} skipped as bulk / automated, {len(out)} to read")
    return out


# ------------------------------------------------------------------ Fireflies
FF_QUERY = """query T($from: DateTime, $to: DateTime, $limit: Int, $skip: Int) {
  transcripts(fromDate: $from, toDate: $to, limit: $limit, skip: $skip) {
    id title date transcript_url organizer_email participants
    summary { action_items overview }
  }
}"""


def fireflies_post(key, query, variables):
    req = urllib.request.Request(FIREFLIES_URL, data=json.dumps({"query": query, "variables": variables}).encode(),
                                 headers={"Content-Type": "application/json", "Authorization": f"Bearer {key}"})
    with urllib.request.urlopen(req, timeout=60) as r:
        d = json.loads(r.read().decode("utf-8"))
    if d.get("errors"):
        raise RuntimeError("Fireflies: " + "; ".join(e.get("message", "?") for e in d["errors"])[:300])
    return d["data"]


def fetch_meetings(key, owner, start, end, log=print):
    iso = lambda ts: dt.datetime.fromtimestamp(ts, dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")
    rows, skip = [], 0
    while True:
        page = fireflies_post(key, FF_QUERY, {"from": iso(start), "to": iso(end), "limit": 50, "skip": skip})["transcripts"] or []
        rows += page
        if len(page) < 50:
            break
        skip += 50
    out = []
    for t in rows:
        people = [str(p).lower() for p in (t.get("participants") or [])] + [str(t.get("organizer_email") or "").lower()]
        if FIREFLIES_ONLY_MINE and owner and not any(owner.lower() in p for p in people):
            continue
        s = t.get("summary") or {}
        ai = s.get("action_items") or ""
        ai = "\n".join(ai) if isinstance(ai, list) else str(ai)
        if not ai.strip() and not (s.get("overview") or "").strip():
            continue
        when = time.strftime("%Y-%m-%d %H:%M", time.localtime((t.get("date") or 0) / 1000)) if t.get("date") else ""
        out.append({"kind": "meeting", "id": "fireflies:" + str(t["id"]), "link": t.get("transcript_url") or "",
                    "head": f"MEETING {t.get('title', '')} | {when} | organiser {t.get('organizer_email', '')} | "
                            f"participants: {', '.join((t.get('participants') or [])[:12])}",
                    "text": (f"Action items:\n{ai.strip()}\n\nOverview:\n{(s.get('overview') or '').strip()}")[:MEETING_CHARS]})
    log(f"intake: {len(rows)} meetings found, {len(out)} with you and notes to read")
    return out


# ------------------------------------------------------------------ extraction
def batches(items):
    cur, size = [], 0
    for it in items:
        n = len(it["text"]) + len(it["head"]) + 20
        if cur and size + n > BATCH_CHARS:
            yield cur
            cur, size = [], 0
        cur.append(it)
        size += n
    if cur:
        yield cur


def parse_json(text):
    m = re.search(r"\{.*\}", text or "", re.S)
    if not m:
        return []
    try:
        d = json.loads(m.group(0))
    except ValueError:
        return []
    return [t for t in (d.get("tasks") or []) if isinstance(t, dict) and (t.get("title") or t.get("request"))]


def extract(llm, batch, owner, clients):
    ref = {f"{'E' if it['kind'] == 'email' else 'M'}{i}": it for i, it in enumerate(batch, 1)}
    user = "\n\n".join(f"### [{k}] {it['head']}\n{it['text']}" for k, it in ref.items())
    ans = llm.chat(SYSTEM.format(owner=owner or "the user", clients=", ".join(clients) or "-"), user,
                   as_json=True, max_tokens=MAX_TOKENS, kind=MODEL_KIND)
    out = []
    for t in parse_json(ans):
        it = ref.get(str(t.get("source_id", "")).strip("[] ")) or (batch[0] if len(batch) == 1 else None)
        if it:
            out.append((it, t))
    return out


def to_item(it, t, n, clients):
    client = str(t.get("client") or "").strip()
    client = next((c for c in clients if c.lower() == client.lower()), "")
    due = str(t.get("due") or "").strip()
    pr = str(t.get("priority") or "normal").lower()
    return {"title": str(t.get("title") or t.get("request"))[:120], "request": str(t.get("request") or "")[:1000],
            "reason": str(t.get("reason") or "")[:600], "client": client, "requester": str(t.get("requester") or "")[:200],
            "due": due if re.match(r"^\d{4}-\d{2}-\d{2}$", due) else "", "priority": pr if pr in ("high", "normal", "low") else "normal",
            "links": [it["link"]] if it.get("link") else [], "source": it["kind"], "status": TASK_STATUS,
            **({"notes": "Draft reply (edit before sending):\n" + str(t["reply_draft"]).strip()[:3000]}
               if it["kind"] == "email" and str(t.get("reply_draft") or "").strip() else {}),
            "external_id": f"{it['id']}#{n}"}


# ------------------------------------------------------------------ the daily job
class Intake(threading.Thread):
    def __init__(self, llm, tasks, cfg, here, clients_fn, log=print):
        super().__init__(daemon=True)
        self.llm, self.tasks, self.here, self.clients_fn, self.log = llm, tasks, here, clients_fn, log
        c = cfg.get("intake") or {}
        self.enabled = bool(c.get("enabled"))
        self.owner = c.get("owner_email") or ""
        self.ff_key = c.get("fireflies_api_key") or ""
        self.run_at = c.get("run_at") or RUN_AT
        self.busy = False
        with tasks.lock:
            tasks.db.executescript("""
                CREATE TABLE IF NOT EXISTS intake_seen(src TEXT PRIMARY KEY, at INTEGER);
                CREATE TABLE IF NOT EXISTS intake_runs(day TEXT PRIMARY KEY, at INTEGER, emails INTEGER,
                    meetings INTEGER, added INTEGER, ms INTEGER, error TEXT);""")
            tasks.db.commit()

    # -- state
    def last_runs(self, n=7):
        with self.tasks.lock:
            return [dict(zip(("day", "at", "emails", "meetings", "added", "ms", "error"), r)) for r in self.tasks.db.execute(
                "SELECT day, at, emails, meetings, added, ms, error FROM intake_runs ORDER BY day DESC LIMIT ?", (n,))]

    def status(self):
        return {"enabled": self.enabled, "run_at": self.run_at, "busy": self.busy, "owner": self.owner,
                "gmail": os.path.exists(os.path.join(self.here, GMAIL_TOKEN_FILE)), "fireflies": bool(self.ff_key),
                "runs": self.last_runs()}

    def _seen(self, src):
        with self.tasks.lock:
            return bool(self.tasks.db.execute("SELECT 1 FROM intake_seen WHERE src = ?", (src,)).fetchone())

    def _mark(self, srcs):
        with self.tasks.lock:
            self.tasks.db.executemany("INSERT OR IGNORE INTO intake_seen VALUES (?,?)", [(s, int(time.time() * 1000)) for s in srcs])
            self.tasks.db.commit()

    # -- one run
    def run_day(self, day=None):
        """Read one day's mail + meetings (default: yesterday) and add proposed tasks. Safe to re-run:
        mails / meetings already read are skipped."""
        if self.busy:
            return {"error": "already running"}
        self.busy = True
        day = day or (dt.date.today() - dt.timedelta(days=LOOKBACK_DAYS))
        t0, errs, items, added, new = time.time(), [], [], [], []
        try:
            start, end = day_range(day)
            if os.path.exists(os.path.join(self.here, GMAIL_TOKEN_FILE)):
                try:
                    items += fetch_emails(gmail_session(self.here), self.owner, start, end, self.log)
                except Exception as ex:
                    errs.append(f"gmail: {type(ex).__name__}: {ex}"[:300])
            else:
                errs.append("gmail: not signed in")
            if self.ff_key:
                try:
                    items += fetch_meetings(self.ff_key, self.owner, start, end, self.log)
                except Exception as ex:
                    errs.append(f"fireflies: {type(ex).__name__}: {ex}"[:300])
            else:
                errs.append("fireflies: no api key")
            new = [it for it in items if not self._seen(it["id"])]
            try:
                clients = sorted(self.clients_fn() or [])
            except Exception:
                clients = []
            for b in batches(new):
                try:
                    found = extract(self.llm, b, self.owner, clients)
                except Exception as ex:
                    errs.append(f"model: {type(ex).__name__}: {ex}"[:300])
                    continue                       # not marked seen -> tried again next run
                per = {}
                rows = []
                for it, t in found:
                    per[it["id"]] = per.get(it["id"], 0) + 1
                    rows.append(to_item(it, t, per[it["id"]], clients))
                res = self.tasks.add(rows) if rows else {"added": []}
                added += res.get("added", [])
                self._mark([it["id"] for it in b])
            n_mail = sum(1 for i in new if i["kind"] == "email")
            n_meet = len(new) - n_mail
            self.log(f"intake {day}: read {n_mail} emails + {n_meet} meetings -> {len(added)} proposed task(s)"
                     + (f"; problems: {'; '.join(errs)}" if errs else ""))
            if errs:
                _hook("event", "warning", "intake", "daily", "; ".join(errs)[:500])
            return {"day": str(day), "emails": n_mail, "meetings": n_meet, "added": added, "errors": errs}
        finally:
            ms = int((time.time() - t0) * 1000)
            with self.tasks.lock:
                # re-runs of the same day add up (they only read what wasn't read before)
                self.tasks.db.execute(
                    "INSERT INTO intake_runs VALUES (?,?,?,?,?,?,?) ON CONFLICT(day) DO UPDATE SET at = excluded.at, "
                    "emails = emails + excluded.emails, meetings = meetings + excluded.meetings, "
                    "added = added + excluded.added, ms = excluded.ms, error = excluded.error",
                    (str(day), int(time.time() * 1000), sum(1 for i in new if i["kind"] == "email"),
                     sum(1 for i in new if i["kind"] == "meeting"), len(added), ms, "; ".join(errs)[:1000] or None))
                self.tasks.db.commit()
            self.busy = False

    # -- scheduler: once a day after RUN_AT
    def run(self):
        if not self.enabled:
            return
        time.sleep(90)                      # let the dashboard finish starting first
        while True:
            try:
                now = dt.datetime.now()
                hh, mm = (int(x) for x in self.run_at.split(":"))
                day = (now.date() - dt.timedelta(days=LOOKBACK_DAYS))
                # done for the day, unless it had problems: then try again every RETRY_MINUTES
                done = any(r["day"] == str(day) and (not r["error"] or time.time() * 1000 - r["at"] < RETRY_MINUTES * 60_000)
                           for r in self.last_runs(3))
                if not done and (now.hour, now.minute) >= (hh, mm):
                    self.run_day(day)
            except Exception as ex:
                self.log(f"intake failed: {type(ex).__name__}: {ex}")
                _hook("event", "error", "intake", "daily", f"{type(ex).__name__}: {ex}"[:500])
            time.sleep(60)
