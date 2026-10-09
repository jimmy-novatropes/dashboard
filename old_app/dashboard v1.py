#!/usr/bin/env python3
"""
AWS Error Feed — quick & dirty multi-account CloudWatch Logs error watcher.

Polls CloudWatch Logs in every AWS profile you have (~/.aws/config + credentials),
looks for errors / tracebacks / exceptions, and shows them in a live local feed
with desktop notifications.

    pip install boto3
    python aws_error_feed.py                 # all profiles, their default region
    python aws_error_feed.py -c config.json  # pick profiles/regions/log groups

Then it opens http://127.0.0.1:8765 in your browser. Click "Enable notifications"
once so errors pop up on your desktop.

Needs only read access in each account: logs:DescribeLogGroups, logs:FilterLogEvents.
"""
import argparse
import collections
import json
import os
import sys
import threading
import time
import traceback
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

try:
    import boto3
    from botocore.config import Config as BotoConfig
except ImportError:
    sys.exit("boto3 is required:  pip install boto3")

# --------------------------------------------------------------------------- config

DEFAULTS = {
    "profiles": [],              # [] = every profile found on this machine
    "exclude_profiles": [],      # profiles to skip
    "regions": [],               # [] = each profile's own default region (fallback us-east-1)
    "log_group_prefixes": [],    # [] = all log groups; e.g. ["/aws/lambda/", "/ecs/"]
    "exclude_log_group_substrings": [],
    # CloudWatch filter pattern syntax; terms are case-sensitive, ? = OR
    "filter_pattern": '?ERROR ?Error ?Traceback ?Exception ?CRITICAL ?FATAL ?"Task timed out"',
    "poll_seconds": 30,
    "lookback_minutes": 60,      # how far back to look on startup
    "group_refresh_minutes": 10,
    "max_groups_per_region": 300,
    "max_events_in_memory": 3000,
    "port": 8765,
}


def load_config(path):
    cfg = dict(DEFAULTS)
    if path:
        with open(path) as f:
            cfg.update(json.load(f))
    return cfg


# --------------------------------------------------------------------------- store

class Store:
    """Thread-safe in-memory feed + per-worker status."""

    def __init__(self, max_events):
        self.lock = threading.Lock()
        self.events = collections.deque(maxlen=max_events)
        self.seq = 0
        self.seen = collections.OrderedDict()   # eventId -> None (bounded dedupe)
        self.status = {}                        # "profile/region" -> dict

    def add(self, ev):
        with self.lock:
            key = ev["id"]
            if key in self.seen:
                return False
            self.seen[key] = None
            if len(self.seen) > 50000:
                self.seen.popitem(last=False)
            self.seq += 1
            ev["seq"] = self.seq
            self.events.append(ev)
            return True

    def since(self, seq):
        with self.lock:
            return [e for e in self.events if e["seq"] > seq], self.seq

    def set_status(self, key, **kw):
        with self.lock:
            self.status.setdefault(key, {}).update(kw, updated=time.time())

    def get_status(self):
        with self.lock:
            return json.loads(json.dumps(self.status))


def classify(msg):
    if "Traceback" in msg or "\tat " in msg or "stackTrace" in msg:
        return "traceback"
    if "Task timed out" in msg:
        return "timeout"
    if "CRITICAL" in msg or "FATAL" in msg:
        return "critical"
    return "error"


# --------------------------------------------------------------------------- worker

class Worker(threading.Thread):
    """Polls one (profile, region) pair."""

    def __init__(self, profile, region, cfg, store):
        super().__init__(daemon=True)
        self.profile, self.region, self.cfg, self.store = profile, region, cfg, store
        self.key = f"{profile}/{region}"
        self.groups = []
        self.groups_refreshed = 0
        self.cursor = int((time.time() - cfg["lookback_minutes"] * 60) * 1000)
        self.account = "?"

    def client(self):
        session = boto3.Session(profile_name=self.profile, region_name=self.region)
        try:
            self.account = session.client("sts").get_caller_identity()["Account"]
        except Exception:
            pass
        return session.client(
            "logs", config=BotoConfig(retries={"max_attempts": 8, "mode": "adaptive"})
        )

    def refresh_groups(self, logs):
        prefixes = self.cfg["log_group_prefixes"] or [None]
        excl = self.cfg["exclude_log_group_substrings"]
        groups = []
        pager = logs.get_paginator("describe_log_groups")
        for p in prefixes:
            kw = {"logGroupNamePrefix": p} if p else {}
            for page in pager.paginate(**kw):
                for g in page.get("logGroups", []):
                    name = g["logGroupName"]
                    if not any(s in name for s in excl):
                        groups.append(name)
        self.groups = sorted(set(groups))[: self.cfg["max_groups_per_region"]]
        self.groups_refreshed = time.time()

    def poll(self, logs):
        start = self.cursor - 120_000          # 2-min overlap for late ingestion; deduped
        poll_started = int(time.time() * 1000)
        new = 0
        for group in self.groups:
            try:
                pager = logs.get_paginator("filter_log_events")
                pages = pager.paginate(
                    logGroupName=group,
                    startTime=start,
                    filterPattern=self.cfg["filter_pattern"],
                    PaginationConfig={"MaxItems": 200, "PageSize": 100},
                )
                for page in pages:
                    for e in page.get("events", []):
                        msg = e.get("message", "").rstrip()
                        ok = self.store.add({
                            "id": f"{self.key}:{e['eventId']}",
                            "ts": e["timestamp"],
                            "profile": self.profile,
                            "account": self.account,
                            "region": self.region,
                            "group": group,
                            "stream": e.get("logStreamName", ""),
                            "kind": classify(msg),
                            "message": msg[:20000],
                        })
                        new += ok
            except logs.exceptions.ResourceNotFoundException:
                continue
            except Exception as ex:  # one bad group shouldn't kill the loop
                self.store.set_status(self.key, last_group_error=f"{group}: {ex}")
        self.cursor = poll_started
        return new

    def run(self):
        logs = None
        while True:
            try:
                if logs is None:
                    logs = self.client()
                    self.store.set_status(self.key, profile=self.profile, region=self.region,
                                          account=self.account)
                if time.time() - self.groups_refreshed > self.cfg["group_refresh_minutes"] * 60:
                    self.refresh_groups(logs)
                t0 = time.time()
                new = self.poll(logs)
                self.store.set_status(self.key, ok=True, error=None, groups=len(self.groups),
                                      last_poll=time.time(), poll_secs=round(time.time() - t0, 1),
                                      new_last_poll=new)
            except Exception as ex:
                # Typical: expired SSO session -> run `aws sso login --profile X`
                logs = None
                self.store.set_status(self.key, ok=False, profile=self.profile,
                                      region=self.region, error=str(ex).splitlines()[0][:300])
            time.sleep(self.cfg["poll_seconds"])


def build_workers(cfg, store):
    base = boto3.Session()
    profiles = cfg["profiles"] or base.available_profiles or ["default"]
    profiles = [p for p in profiles if p not in cfg["exclude_profiles"]]
    workers = []
    for p in profiles:
        regions = cfg["regions"]
        if not regions:
            try:
                regions = [boto3.Session(profile_name=p).region_name or "us-east-1"]
            except Exception:
                regions = ["us-east-1"]
        for r in regions:
            workers.append(Worker(p, r, cfg, store))
    return workers


# --------------------------------------------------------------------------- web

def make_handler(store):
    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def send(self, code, body, ctype):
            data = body.encode() if isinstance(body, str) else body
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self):
            u = urlparse(self.path)
            if u.path == "/":
                return self.send(200, PAGE, "text/html; charset=utf-8")
            if u.path == "/api/events":
                since = int(parse_qs(u.query).get("since", ["0"])[0])
                events, seq = store.since(since)
                return self.send(200, json.dumps({"events": events, "seq": seq}),
                                 "application/json")
            if u.path == "/api/status":
                return self.send(200, json.dumps(store.get_status()), "application/json")
            self.send(404, "not found", "text/plain")

    return H


PAGE = r"""<!doctype html>
<html><head><meta charset="utf-8"><title>AWS Error Feed</title>
<meta name="viewport" content="width=device-width,initial-scale=1">
<style>
:root{--bg:#0f1115;--panel:#171a21;--line:#262b36;--fg:#e6e8ee;--mute:#8a91a3;
--err:#ff6b6b;--tb:#ffa94d;--to:#b197fc;--crit:#ff3b6b;--ok:#51cf66}
@media (prefers-color-scheme:light){:root{--bg:#f6f7f9;--panel:#fff;--line:#e3e6ec;--fg:#1b1f29;--mute:#667085}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--fg);font:14px/1.45 system-ui,sans-serif}
header{position:sticky;top:0;z-index:2;background:var(--panel);border-bottom:1px solid var(--line);padding:10px 16px}
h1{font-size:16px;margin:0 12px 0 0;display:inline}
.row{display:flex;gap:8px;flex-wrap:wrap;align-items:center}
input,select,button{font:inherit;background:var(--bg);color:var(--fg);border:1px solid var(--line);border-radius:6px;padding:5px 9px}
button{cursor:pointer}
#status{margin-top:8px;display:flex;gap:6px;flex-wrap:wrap}
.chip{font-size:12px;padding:2px 8px;border-radius:99px;border:1px solid var(--line);color:var(--mute)}
.chip.bad{border-color:var(--err);color:var(--err)}.chip.good b{color:var(--ok)}
main{padding:12px 16px;max-width:1200px;margin:0 auto}
.ev{background:var(--panel);border:1px solid var(--line);border-left:4px solid var(--err);border-radius:8px;margin-bottom:8px;padding:8px 12px}
.ev.traceback{border-left-color:var(--tb)}.ev.timeout{border-left-color:var(--to)}.ev.critical{border-left-color:var(--crit)}
.ev.new{animation:flash 2s}@keyframes flash{from{background:rgba(255,107,107,.18)}}
.meta{display:flex;gap:10px;flex-wrap:wrap;font-size:12px;color:var(--mute)}
.meta b{color:var(--fg)}.meta a{color:inherit}
.first{margin-top:4px;font-family:ui-monospace,Menlo,monospace;font-size:13px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;cursor:pointer}
pre{display:none;margin:6px 0 0;white-space:pre-wrap;word-break:break-word;font-size:12px;max-height:500px;overflow:auto;background:var(--bg);padding:8px;border-radius:6px}
.ev.open pre{display:block}.ev.open .first{white-space:normal}
.empty{color:var(--mute);text-align:center;padding:40px}
</style></head><body>
<header>
 <div class="row">
  <h1>AWS Error Feed</h1>
  <select id="fProfile"><option value="">All profiles</option></select>
  <select id="fKind"><option value="">All kinds</option><option>traceback</option><option>error</option><option>timeout</option><option>critical</option></select>
  <input id="q" placeholder="Search message / log group…" size="30">
  <button id="pause">Pause</button>
  <button id="notif">Enable notifications</button>
  <span id="count" class="chip"></span>
 </div>
 <div id="status"></div>
</header>
<main><div id="feed"></div><div id="empty" class="empty">Watching… errors will appear here.</div></main>
<script>
let seq=0, all=[], paused=false, firstLoad=true;
const $=id=>document.getElementById(id);
const esc=s=>s.replace(/[&<>"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));
const enc=s=>encodeURIComponent(encodeURIComponent(s)).replace(/%/g,'$');
const consoleUrl=e=>`https://${e.region}.console.aws.amazon.com/cloudwatch/home?region=${e.region}#logsV2:log-groups/log-group/${enc(e.group)}/log-events/${enc(e.stream)}`;
const firstLine=m=>{const l=m.split('\n').map(x=>x.trim()).filter(Boolean);
  const tb=l.findIndex(x=>/^\w*(Error|Exception)\b/.test(x)); return (tb>=0?l[tb]:l[0])||m};

function matches(e){
  const p=$('fProfile').value,k=$('fKind').value,q=$('q').value.toLowerCase();
  return (!p||e.profile===p)&&(!k||e.kind===k)&&(!q||(e.message+e.group).toLowerCase().includes(q));
}
function card(e,isNew){
  const d=document.createElement('div'); d.className=`ev ${e.kind}${isNew?' new':''}`;
  d.innerHTML=`<div class="meta"><span>${new Date(e.ts).toLocaleString()}</span>
   <b>${esc(e.profile)}</b><span>${esc(e.account)} · ${esc(e.region)}</span>
   <span>${esc(e.group)}</span><span>${e.kind}</span>
   <a href="${consoleUrl(e)}" target="_blank">open in console ↗</a></div>
   <div class="first">${esc(firstLine(e.message))}</div><pre>${esc(e.message)}</pre>`;
  d.querySelector('.first').onclick=()=>d.classList.toggle('open');
  return d;
}
function render(newIds){
  const f=$('feed'); f.innerHTML='';
  const list=all.filter(matches).sort((a,b)=>b.ts-a.ts).slice(0,500);
  list.forEach(e=>f.appendChild(card(e,newIds instanceof Set&&newIds.has(e.id))));
  $('empty').style.display=list.length?'none':'block';
  $('count').textContent=`${list.length} shown / ${all.length} total`;
}
function notify(evs){
  if(!("Notification" in window)||Notification.permission!=="granted"||!evs.length)return;
  if(evs.length>3){new Notification(`${evs.length} new AWS errors`,{body:[...new Set(evs.map(e=>e.profile))].join(', ')});return;}
  evs.forEach(e=>new Notification(`[${e.profile}] ${e.group.split('/').pop()}`,{body:firstLine(e.message).slice(0,200),tag:e.id}));
}
async function tick(){
  if(paused)return;
  try{
    const r=await (await fetch('/api/events?since='+seq)).json();
    seq=r.seq;
    if(r.events.length){
      all.push(...r.events); if(all.length>5000)all=all.slice(-5000);
      const profs=[...new Set(all.map(e=>e.profile))].sort(), sel=$('fProfile'), cur=sel.value;
      sel.innerHTML='<option value="">All profiles</option>'+profs.map(p=>`<option>${esc(p)}</option>`).join(''); sel.value=cur;
      if(!firstLoad){ const fresh=r.events.filter(matches); notify(fresh); render(new Set(fresh.map(e=>e.id)));
        if(fresh.length) document.title=`(${fresh.length}) AWS Error Feed`; setTimeout(()=>document.title='AWS Error Feed',8000);
      } else render();
    }
    firstLoad=false;
  }catch(e){}
}
async function status(){
  try{
    const s=await (await fetch('/api/status')).json();
    $('status').innerHTML=Object.entries(s).map(([k,v])=>v.ok
      ?`<span class="chip good" title="${v.groups} log groups, poll ${v.poll_secs}s"><b>●</b> ${esc(k)} (${esc(v.account||'?')}) · ${v.groups} groups</span>`
      :`<span class="chip bad" title="${esc(v.error||'starting…')}">● ${esc(k)} — ${esc((v.error||'starting…').slice(0,90))}</span>`).join('');
  }catch(e){}
}
['fProfile','fKind'].forEach(id=>$(id).onchange=render); $('q').oninput=render;
$('pause').onclick=()=>{paused=!paused;$('pause').textContent=paused?'Resume':'Pause'};
$('notif').onclick=()=>Notification.requestPermission().then(p=>$('notif').textContent=p==='granted'?'Notifications on':'Notifications blocked');
if(window.Notification&&Notification.permission==='granted')$('notif').textContent='Notifications on';
tick();status();setInterval(tick,5000);setInterval(status,10000);
</script></body></html>"""


# --------------------------------------------------------------------------- main

def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("-c", "--config", help="path to config.json (optional)")
    ap.add_argument("--port", type=int)
    ap.add_argument("--no-browser", action="store_true")
    args = ap.parse_args()

    cfg = load_config(args.config)
    if args.port:
        cfg["port"] = args.port

    store = Store(cfg["max_events_in_memory"])
    workers = build_workers(cfg, store)
    if not workers:
        sys.exit("No AWS profiles found. Check ~/.aws/config and ~/.aws/credentials.")
    print(f"Watching {len(workers)} profile/region pair(s):")
    for w in workers:
        print(f"  - {w.key}")
        store.set_status(w.key, ok=False, profile=w.profile, region=w.region, error="starting…")
        w.start()

    url = f"http://127.0.0.1:{cfg['port']}"
    server = ThreadingHTTPServer(("127.0.0.1", cfg["port"]), make_handler(store))
    print(f"Feed: {url}   (Ctrl+C to stop)")
    if not args.no_browser:
        threading.Timer(1.0, lambda: webbrowser.open(url)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nbye")


if __name__ == "__main__":
    main()