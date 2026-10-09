"""
Infrastructure scan for the AWS Error Feed (imported by aws_error_feed.py).

Per account/region, in its own background thread, every `infra_refresh_minutes`:
  - Lambda functions: runtime, timeout, memory, architecture, layers
  - Lambda layers: latest version available + the Python packages inside each version
  - EventBridge Scheduler schedules and EventBridge (CloudWatch Events) scheduled rules
  - Secrets Manager: metadata only (names, last changed / rotated / accessed). Secret VALUES
    are never read. Function environment values are only compared in memory against secret
    names to see which function uses which secret; they are not stored.
  - API Gateway (REST + HTTP APIs): routes -> Lambda, and 24h request / 4xx / 5xx / latency
  - CloudWatch alarms, and Lambda throttles / concurrency
Then analyses combine that with the log lines the dashboard already has (Lambda REPORT lines):
missed scheduled runs, timeout / memory headroom, cost estimate, secrets changed before auth
errors, and a per-client daily digest.

All calls are read-only. A missing permission only disables that one section for that account.
"""
import datetime
import io
import json
import re
import threading
import time
import urllib.request
import zipfile

import boto3
from botocore.config import Config as BotoConfig

HOUR_MS = 3_600_000
DAY_MS = 86_400_000

# --------------------------------------------------------------------------- runtimes
# From https://docs.aws.amazon.com/lambda/latest/dg/lambda-runtimes.html (checked Oct 2026).
# identifier: (deprecation, block function create, block function update); None = not scheduled
RUNTIMES = {
    "nodejs24.x": ("2028-04-30", "2028-06-01", "2028-07-01"),
    "nodejs22.x": ("2027-04-30", "2027-06-01", "2027-07-01"),
    "nodejs20.x": ("2026-04-30", "2027-07-29", "2027-08-31"),
    "nodejs18.x": ("2025-09-01", "2027-07-29", "2027-08-31"),
    "nodejs16.x": ("2024-06-12", "2027-07-29", "2027-08-31"),
    "nodejs14.x": ("2023-12-04", "2024-01-09", "2027-08-31"),
    "python3.14": ("2029-06-30", "2029-07-31", "2029-08-31"),
    "python3.13": ("2029-06-30", "2029-07-31", "2029-08-31"),
    "python3.12": ("2028-10-31", "2028-11-30", "2029-01-10"),
    "python3.11": ("2027-06-30", "2027-07-29", "2027-08-31"),
    "python3.10": ("2026-10-31", "2027-07-29", "2027-08-31"),
    "python3.9": ("2025-12-15", "2027-07-29", "2027-08-31"),
    "python3.8": ("2024-10-14", "2027-07-29", "2027-08-31"),
    "python3.7": ("2023-12-04", "2024-01-09", "2027-08-31"),
    "java21": ("2029-06-30", "2029-07-31", "2029-08-31"),
    "java17": ("2027-06-30", "2027-07-29", "2027-08-31"),
    "java11": ("2027-06-30", "2027-07-29", "2027-08-31"),
    "java8.al2": ("2027-06-30", "2027-07-29", "2027-08-31"),
    "java8": ("2024-01-08", "2024-02-08", "2027-08-31"),
    "dotnet10": ("2028-11-14", "2028-12-14", "2029-01-15"),
    "dotnet9": ("2026-11-10", None, None),
    "dotnet8": ("2026-11-10", "2027-07-29", "2027-08-31"),
    "dotnet6": ("2024-12-20", "2027-07-29", "2027-08-31"),
    "ruby3.4": ("2028-03-31", "2028-04-30", "2028-05-31"),
    "ruby3.3": ("2027-03-31", "2027-07-29", "2027-08-31"),
    "ruby3.2": ("2026-03-31", "2027-07-29", "2027-08-31"),
    "provided.al2023": ("2029-06-30", "2029-07-31", "2029-08-31"),
    "provided.al2": ("2026-07-31", "2027-07-29", "2027-08-31"),
    "go1.x": ("2024-01-08", "2024-02-08", "2027-08-31"),
    "provided": ("2024-01-08", "2024-02-08", "2027-08-31"),
}


def runtime_status(runtime, now_ms=None):
    """('deprecated' | 'soon' | 'ok' | 'unknown' | 'container', human text)."""
    if not runtime:
        return "unknown", "no runtime (container image?)"
    if runtime == "Image":
        return "container", "container image - runtime managed in the image"
    d = RUNTIMES.get(runtime)
    if not d:
        return ("ok", "not scheduled") if re.match(r"(nodejs2[6-9]|python3\.1[5-9])", runtime) else ("unknown", "not in table")
    now = datetime.date.fromtimestamp((now_ms or time.time() * 1000) / 1000)
    dep = datetime.date.fromisoformat(d[0])
    upd = datetime.date.fromisoformat(d[2]) if d[2] else None
    if dep <= now:
        return "deprecated", (f"deprecated since {d[0]}; no security patches."
                              + (f" Code updates blocked from {d[2]}." if upd else ""))
    if (dep - now).days <= 180:
        return "soon", f"deprecated on {d[0]} ({(dep - now).days} days)"
    return "ok", f"supported until {d[0]}"


# --------------------------------------------------------------------------- schedules
_MONTHS = {m: i for i, m in enumerate("JAN FEB MAR APR MAY JUN JUL AUG SEP OCT NOV DEC".split(), 1)}
_DAYS = {d: i for i, d in enumerate("SUN MON TUE WED THU FRI SAT".split(), 1)}   # AWS: 1 = Sunday


class Unsupported(Exception):
    pass


def _field(spec, lo, hi, names=None):
    """One cron field -> set of ints (None for '?')."""
    if spec == "?":
        return None
    out = set()
    for part in spec.upper().split(","):
        if any(c in part for c in "LW#"):
            raise Unsupported(spec)
        step = 1
        if "/" in part:
            part, step = part.split("/")
            step = int(step)
        if part in ("*", ""):
            a, b = lo, hi
        elif "-" in part:
            a, b = part.split("-")
            a, b = (names or {}).get(a) or int(a), (names or {}).get(b) or int(b)
        else:
            a = (names or {}).get(part) or int(part)
            b = hi if step > 1 else a
        out.update(range(a, b + 1, step))
    return out


def parse_schedule(expr):
    """('rate', seconds) | ('cron', fields) | ('at', datetime-naive) | (None, reason)."""
    expr = (expr or "").strip()
    m = re.match(r"rate\((\d+)\s+(minute|minutes|hour|hours|day|days)\)", expr)
    if m:
        n, unit = int(m.group(1)), m.group(2)
        return "rate", n * {"m": 60, "h": 3600, "d": 86400}[unit[0]]
    m = re.match(r"at\((\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d)\)", expr)
    if m:
        return "at", datetime.datetime.fromisoformat(m.group(1))
    m = re.match(r"cron\((.+)\)", expr)
    if m:
        parts = m.group(1).split()
        if len(parts) != 6:
            return None, "cron needs 6 fields"
        try:
            return "cron", {
                "minute": _field(parts[0], 0, 59), "hour": _field(parts[1], 0, 23),
                "dom": _field(parts[2], 1, 31), "month": _field(parts[3], 1, 12, _MONTHS),
                "dow": _field(parts[4], 1, 7, _DAYS), "year": _field(parts[5], 1970, 2199)}
        except (Unsupported, ValueError):
            return None, "uses L / W / # (not checked)"
    return None, "unknown expression"


def _tz(name):
    if not name or name == "UTC":
        return datetime.timezone.utc, True
    try:
        from zoneinfo import ZoneInfo
        return ZoneInfo(name), True
    except Exception:            # Windows without the tzdata package
        return datetime.timezone.utc, False


def cron_times(fields, start_ms, end_ms, tzname="UTC", limit=5000):
    """Fire times (epoch ms) of an AWS cron expression in [start, end)."""
    tz, _ = _tz(tzname)
    start = datetime.datetime.fromtimestamp(start_ms / 1000, tz)
    end = datetime.datetime.fromtimestamp(end_ms / 1000, tz)
    day = start.date()
    out = []
    while day <= end.date() and len(out) < limit:
        aws_dow = (day.isoweekday() % 7) + 1          # Mon=1..Sun=7 -> Sun=1..Sat=7
        ok = (fields["month"] is None or day.month in fields["month"]) and \
             (fields["year"] is None or day.year in fields["year"])
        if ok:
            if fields["dom"] is not None and fields["dow"] is None:
                ok = day.day in fields["dom"]
            elif fields["dow"] is not None and fields["dom"] is None:
                ok = aws_dow in fields["dow"]
            elif fields["dom"] is not None and fields["dow"] is not None:
                ok = day.day in fields["dom"] or aws_dow in fields["dow"]
        if ok:
            for h in sorted(fields["hour"]):
                for mi in sorted(fields["minute"]):
                    dt = datetime.datetime(day.year, day.month, day.day, h, mi, tzinfo=tz)
                    ms = int(dt.timestamp() * 1000)
                    if start_ms <= ms < end_ms:
                        out.append(ms)
        day += datetime.timedelta(days=1)
    return out


def next_run(kind, val, tzname, now_ms, created_ms=None):
    if kind == "cron":
        t = cron_times(val, now_ms, now_ms + 400 * DAY_MS, tzname, limit=1)
        return t[0] if t else None
    if kind == "rate":
        base = created_ms or now_ms
        step = val * 1000
        return base + ((now_ms - base) // step + 1) * step
    if kind == "at":
        tz, _ = _tz(tzname)
        ms = int(val.replace(tzinfo=tz).timestamp() * 1000)
        return ms if ms > now_ms else None
    return None


# --------------------------------------------------------------------------- REPORT lines
REPORT_RX = re.compile(
    r"REPORT RequestId:\s*(\S+)\s+Duration:\s*([\d.]+) ms\s+Billed Duration:\s*(\d+) ms\s+"
    r"Memory Size:\s*(\d+) MB\s+Max Memory Used:\s*(\d+) MB(?:\s+Init Duration:\s*([\d.]+) ms)?(.*)", re.S)
STATUS_RX = re.compile(r"Status:\s*(\w+)")


def report_stats(store, profile, region, since_ms):
    """Per log group: invocation times + duration / memory / billing from Lambda REPORT lines."""
    with store.lock:
        rows = store.db.execute(
            "SELECT grp, ts, message FROM events WHERE profile = ? AND region = ? AND level = 'platform' "
            "AND ts >= ? AND message LIKE 'REPORT%' ORDER BY ts", (profile, region, since_ms)).fetchall()
    out = {}
    for grp, ts, msg in rows:
        m = REPORT_RX.match(msg)
        if not m:
            continue
        s = out.setdefault(grp, {"runs": [], "n": 0, "dur_sum": 0.0, "dur_max": 0.0, "billed_ms": 0,
                                 "mem_size": 0, "mem_max": 0, "timeouts": 0, "status_errors": 0,
                                 "cold_starts": 0, "last_status": None})
        dur, billed, size, used = float(m.group(2)), int(m.group(3)), int(m.group(4)), int(m.group(5))
        st = STATUS_RX.search(m.group(7) or "")
        status = st.group(1).lower() if st else "success"
        s["runs"].append((ts, dur))
        s["n"] += 1
        s["dur_sum"] += dur
        s["dur_max"] = max(s["dur_max"], dur)
        s["billed_ms"] += billed
        s["mem_size"] = size
        s["mem_max"] = max(s["mem_max"], used)
        s["cold_starts"] += 1 if m.group(6) else 0
        s["timeouts"] += status == "timeout"
        s["status_errors"] += status not in ("success", "timeout")
        s["last_status"] = status
    return out


# --------------------------------------------------------------------------- scanner
def _ms(v):
    if v is None:
        return None
    if isinstance(v, (int, float)):
        return int(v)
    return int(v.timestamp() * 1000)


def _err(ex):
    t = str(ex).splitlines()[0]
    if "AccessDenied" in t or "not authorized" in t or "UnauthorizedOperation" in t:
        return "no permission"
    return t[:200]


def _fn_name(arn):
    m = re.match(r"arn:aws[\w-]*:lambda:[^:]+:\d+:function:([^:]+)", arn or "")
    return m.group(1) if m else None


def _sm_name(arn):
    m = re.match(r"arn:aws[\w-]*:states:[^:]+:\d+:stateMachine:([^:]+)", arn or "")
    return m.group(1) if m else None


class InfraScanner(threading.Thread):
    def __init__(self, profile, region, cfg, store):
        super().__init__(daemon=True)
        self.profile, self.region, self.cfg, self.store = profile, region, cfg, store
        self.key = f"{profile}/{region}"
        self.wake = threading.Event()
        self.scanning = False

    def run(self):
        time.sleep(20)                          # let the log polling start first
        while True:
            try:
                self.scanning = True
                self.scan()
            except Exception as ex:             # never let the thread die
                snap = self.store.get_infra(self.key) or {}
                snap.setdefault("errors", {})["scan"] = _err(ex)
                self.store.save_infra(self.key, snap)
            finally:
                self.scanning = False
            self.wake.wait(self.cfg["infra_refresh_minutes"] * 60)
            self.wake.clear()

    # ---------------------------------------------------------------- scan
    def scan(self):
        s = boto3.Session(profile_name=self.profile, region_name=self.region)
        bc = BotoConfig(retries={"max_attempts": 6, "mode": "adaptive"})
        c = lambda name: s.client(name, config=bc)
        now = int(time.time() * 1000)
        snap = {"at": now, "profile": self.profile, "region": self.region, "errors": {},
                "functions": [], "layers": {}, "secrets": [], "schedules": [], "apis": [], "alarms": []}
        envs = {}

        # ---- Lambda functions
        try:
            for page in c("lambda").get_paginator("list_functions").paginate():
                for f in page.get("Functions", []):
                    envs[f["FunctionName"]] = (f.get("Environment") or {}).get("Variables") or {}
                    snap["functions"].append({
                        "name": f["FunctionName"], "arn": f["FunctionArn"],
                        "runtime": f.get("Runtime") or ("Image" if f.get("PackageType") == "Image" else ""),
                        "timeout": f.get("Timeout", 3), "memory": f.get("MemorySize", 128),
                        "arch": (f.get("Architectures") or ["x86_64"])[0],
                        "layers": [l["Arn"] for l in f.get("Layers", [])],
                        "log_group": (f.get("LoggingConfig") or {}).get("LogGroup") or "/aws/lambda/" + f["FunctionName"],
                        "dlq": bool((f.get("DeadLetterConfig") or {}).get("TargetArn")),
                        "secrets": []})
        except Exception as ex:
            snap["errors"]["functions"] = _err(ex)

        # ---- Secrets (metadata only)
        try:
            for page in c("secretsmanager").get_paginator("list_secrets").paginate():
                for x in page.get("SecretList", []):
                    snap["secrets"].append({
                        "name": x["Name"], "arn": x["ARN"], "created": _ms(x.get("CreatedDate")),
                        "last_changed": _ms(x.get("LastChangedDate")), "last_rotated": _ms(x.get("LastRotatedDate")),
                        "last_accessed": _ms(x.get("LastAccessedDate")),
                        "rotation": bool(x.get("RotationEnabled")), "next_rotation": _ms(x.get("NextRotationDate")),
                        "functions": []})
        except Exception as ex:
            snap["errors"]["secrets"] = _err(ex)
        # which function references which secret (env values compared in memory, never stored)
        for f in snap["functions"]:
            vals = [str(v) for v in envs.get(f["name"], {}).values()]
            for sec in snap["secrets"]:
                arn_base = sec["arn"].rsplit("-", 1)[0]
                if any(v == sec["name"] or v.endswith("/" + sec["name"]) or arn_base in v for v in vals):
                    f["secrets"].append(sec["name"])
                    sec["functions"].append(f["name"])
        envs.clear()

        # ---- Layers: latest version + packages inside each used version
        used = sorted({l for f in snap["functions"] for l in f["layers"]})
        lam = c("lambda")
        for arn in used:
            base, ver = arn.rsplit(":", 1)
            info = snap["layers"].setdefault(base, {"name": base.split(":")[-1], "used_versions": {},
                                                    "latest": None, "error": None})
            info["used_versions"].setdefault(ver, [])
            if info["latest"] is None and info["error"] is None:
                try:
                    vs = lam.list_layer_versions(LayerName=base).get("LayerVersions", []) or \
                        lam.list_layer_versions(LayerName=info["name"]).get("LayerVersions", [])
                    info["latest"] = max((v["Version"] for v in vs), default=None)
                except Exception as ex:
                    info["error"] = _err(ex)
            if self.cfg["infra_scan_layer_packages"] and self.store.get_layer_pkgs(arn) is None:
                self.store.save_layer_pkgs(arn, self.layer_packages(lam, arn))
        for f in snap["functions"]:
            for arn in f["layers"]:
                base, ver = arn.rsplit(":", 1)
                snap["layers"][base]["used_versions"][ver].append(f["name"])

        # ---- Schedules: EventBridge Scheduler + scheduled rules
        try:
            sch = c("scheduler")
            for page in sch.get_paginator("list_schedules").paginate():
                for x in page.get("Schedules", []):
                    d = sch.get_schedule(Name=x["Name"], GroupName=x.get("GroupName", "default"))
                    tgt = d.get("Target", {})
                    target_arn, sm = tgt.get("Arn", ""), None
                    if "aws-sdk:sfn:startExecution" in target_arn:
                        try:
                            sm = json.loads(tgt.get("Input") or "{}").get("StateMachineArn")
                        except ValueError:
                            pass
                    snap["schedules"].append({
                        "source": "scheduler", "name": x["Name"], "group": x.get("GroupName", "default"),
                        "expression": d.get("ScheduleExpression"), "tz": d.get("ScheduleExpressionTimezone") or "UTC",
                        "state": d.get("State"), "created": _ms(d.get("CreationDate")),
                        "flex_minutes": (d.get("FlexibleTimeWindow") or {}).get("MaximumWindowInMinutes") or 0,
                        "function": _fn_name(target_arn), "state_machine": _sm_name(sm or target_arn),
                        "state_machine_arn": sm or (target_arn if ":stateMachine:" in target_arn else None),
                        "target": target_arn, "dlq": bool((tgt.get("DeadLetterConfig") or {}).get("Arn")),
                        "retries": (tgt.get("RetryPolicy") or {}).get("MaximumRetryAttempts")})
        except Exception as ex:
            snap["errors"]["scheduler"] = _err(ex)
        try:
            ev = c("events")
            for page in ev.get_paginator("list_rules").paginate():
                for r in page.get("Rules", []):
                    if not r.get("ScheduleExpression"):
                        continue
                    tg = ev.list_targets_by_rule(Rule=r["Name"]).get("Targets", [])
                    for t in tg or [{}]:
                        snap["schedules"].append({
                            "source": "rule", "name": r["Name"], "group": r.get("EventBusName", "default"),
                            "expression": r["ScheduleExpression"], "tz": "UTC", "state": r.get("State"),
                            "created": None, "flex_minutes": 0, "function": _fn_name(t.get("Arn")),
                            "state_machine": _sm_name(t.get("Arn")), "target": t.get("Arn", "(no target)"),
                            "state_machine_arn": t.get("Arn") if ":stateMachine:" in (t.get("Arn") or "") else None,
                            "dlq": bool((t.get("DeadLetterConfig") or {}).get("Arn")),
                            "retries": (t.get("RetryPolicy") or {}).get("MaximumRetryAttempts")})
        except Exception as ex:
            snap["errors"]["eventbridge_rules"] = _err(ex)
        # Step Functions targets: when did their executions actually start (last 24h)
        sfn = None
        for sc in snap["schedules"]:
            if not sc.get("state_machine_arn"):
                continue
            try:
                sfn = sfn or c("stepfunctions")
                arn = sc["state_machine_arn"]
                starts = []
                for page in sfn.get_paginator("list_executions").paginate(
                        stateMachineArn=arn, PaginationConfig={"MaxItems": 2000, "PageSize": 1000}):
                    for e in page.get("executions", []):
                        st = _ms(e.get("startDate"))
                        if st < now - DAY_MS:
                            break
                        starts.append(st)
                sc["sfn_starts"] = sorted(starts)
            except Exception as ex:
                sc["sfn_error"] = _err(ex)

        # ---- API Gateway (REST v1 + HTTP v2) with 24h metrics
        cw = c("cloudwatch")
        try:
            ag = c("apigateway")
            for page in ag.get_paginator("get_rest_apis").paginate():
                for a in page.get("items", []):
                    stages = [x["stageName"] for x in ag.get_stages(restApiId=a["id"]).get("item", [])]
                    routes = []
                    for rp in ag.get_paginator("get_resources").paginate(restApiId=a["id"], embed=["methods"]):
                        for res in rp.get("items", []):
                            for meth, md in (res.get("resourceMethods") or {}).items():
                                uri = (md.get("methodIntegration") or {}).get("uri", "")
                                routes.append({"route": f"{meth} {res.get('path')}", "function": _fn_name(
                                    (re.search(r"functions/(arn:[^/]+)/invocations", uri) or [None, None])[1])})
                    snap["apis"].append({"kind": "REST", "id": a["id"], "name": a["name"], "stages": stages,
                                         "routes": routes[:200]})
        except Exception as ex:
            snap["errors"]["apigateway_rest"] = _err(ex)
        try:
            ag2 = c("apigatewayv2")
            for a in ag2.get_apis().get("Items", []):
                stages = [x["StageName"] for x in ag2.get_stages(ApiId=a["ApiId"]).get("Items", [])]
                integ = {i["IntegrationId"]: i.get("IntegrationUri", "")
                         for i in ag2.get_integrations(ApiId=a["ApiId"]).get("Items", [])}
                routes = []
                for r in ag2.get_routes(ApiId=a["ApiId"]).get("Items", []):
                    iid = (r.get("Target") or "").replace("integrations/", "")
                    routes.append({"route": r.get("RouteKey"), "function": _fn_name(integ.get(iid, ""))})
                snap["apis"].append({"kind": a.get("ProtocolType", "HTTP"), "id": a["ApiId"], "name": a["Name"],
                                     "stages": stages, "routes": routes[:200]})
        except Exception as ex:
            snap["errors"]["apigateway_http"] = _err(ex)
        if snap["apis"]:
            try:
                self.api_metrics(cw, snap["apis"], now)
            except Exception as ex:
                snap["errors"]["api_metrics"] = _err(ex)

        # ---- CloudWatch alarms + Lambda throttles / concurrency
        try:
            for page in cw.get_paginator("describe_alarms").paginate():
                for a in page.get("MetricAlarms", []) + page.get("CompositeAlarms", []):
                    snap["alarms"].append({
                        "name": a["AlarmName"], "state": a.get("StateValue"),
                        "reason": (a.get("StateReason") or "")[:300], "since": _ms(a.get("StateUpdatedTimestamp")),
                        "metric": f"{a.get('Namespace', '')} {a.get('MetricName', '')}".strip() or "composite",
                        "dims": {d["Name"]: d["Value"] for d in a.get("Dimensions", [])}})
        except Exception as ex:
            snap["errors"]["alarms"] = _err(ex)
        if snap["functions"]:
            try:
                self.lambda_metrics(cw, snap["functions"], now)
            except Exception as ex:
                snap["errors"]["lambda_metrics"] = _err(ex)

        # ---- how much each log group normally logs (CloudWatch keeps this metric ~2 weeks),
        # so a function that suddenly goes quiet can be spotted without waiting for a baseline
        try:
            names = []
            for page in c("logs").get_paginator("describe_log_groups").paginate():
                names += [g["logGroupName"] for g in page.get("logGroups", [])]
            snap["log_volume"] = self.log_volume(cw, names[:300], now)
        except Exception as ex:
            snap["errors"]["log_volume"] = _err(ex)

        self.store.save_infra(self.key, snap)

    # ---------------------------------------------------------------- helpers
    def log_volume(self, cw, names, now):
        """Lines per hour for each log group: last 6h / 24h vs the same windows over the 7 days before."""
        q = [{"Id": f"g{i}", "ReturnData": True, "MetricStat": {
            "Metric": {"Namespace": "AWS/Logs", "MetricName": "IncomingLogEvents",
                       "Dimensions": [{"Name": "LogGroupName", "Value": n}]},
            "Period": 3600, "Stat": "Sum"}} for i, n in enumerate(names)]
        start = (now // HOUR_MS - 8 * 24) * HOUR_MS
        out = {}
        for i in range(0, len(q), 450):
            kw = dict(MetricDataQueries=q[i:i + 450], ScanBy="TimestampAscending",
                      StartTime=datetime.datetime.fromtimestamp(start / 1000, datetime.timezone.utc),
                      EndTime=datetime.datetime.fromtimestamp(now / 1000, datetime.timezone.utc))
            while True:
                r = cw.get_metric_data(**kw)
                for m in r.get("MetricDataResults", []):
                    hrs = out.setdefault(names[int(m["Id"][1:])], {})
                    for ts, v in zip(m.get("Timestamps", []), m.get("Values", [])):
                        hrs[int(ts.timestamp() * 1000) // HOUR_MS] = hrs.get(int(ts.timestamp() * 1000) // HOUR_MS, 0) + v
                if not r.get("NextToken"):
                    break
                kw["NextToken"] = r["NextToken"]
        now_h = now // HOUR_MS - 1            # the current hour is still incomplete / delayed
        res = {}
        for n in names:
            hrs = out.get(n, {})
            win = lambda end, length: sum(hrs.get(h, 0) for h in range(end - length + 1, end + 1))
            last6, last24 = win(now_h, 6), win(now_h, 24)
            base6 = sum(win(now_h - 24 * d, 6) for d in range(1, 8)) / 7
            base24 = sum(win(now_h - 24 * d, 24) for d in range(1, 8)) / 7
            last_h = max((h for h, v in hrs.items() if v > 0), default=None)
            res[n] = {"last6": int(last6), "base6": round(base6, 1), "last24": int(last24),
                      "base24": round(base24, 1), "last_active": last_h * HOUR_MS if last_h else None}
        return res
    def layer_packages(self, lam, arn):
        """Python packages inside a layer version (read from *.dist-info names). Cached forever."""
        try:
            loc = lam.get_layer_version_by_arn(Arn=arn).get("Content")
            if not loc:
                base, ver = arn.rsplit(":", 1)
                loc = lam.get_layer_version(LayerName=base, VersionNumber=int(ver))["Content"]
            if loc.get("CodeSize", 0) > self.cfg["infra_max_layer_mb"] * 1_048_576:
                return {"_note": f"skipped: layer is {loc['CodeSize'] // 1_048_576} MB"}
            with urllib.request.urlopen(loc["Location"], timeout=120) as r:
                z = zipfile.ZipFile(io.BytesIO(r.read()))
            pkgs = {}
            for n in z.namelist():
                m = re.search(r"(?:^|/)([A-Za-z0-9_.\-]+?)-(\d[\w.+!-]*)\.(?:dist-info|egg-info)/", n)
                if m:
                    pkgs[m.group(1).replace("_", "-").lower()] = m.group(2)
            nm = [n for n in z.namelist() if re.match(r"nodejs/node_modules/[^/]+/package\.json$", n)]
            for n in nm[:300]:
                try:
                    pj = json.loads(z.read(n))
                    pkgs[pj.get("name", n.split("/")[2])] = pj.get("version", "?")
                except Exception:
                    pass
            return pkgs
        except Exception as ex:
            return {"_note": f"couldn't read: {_err(ex)}"}

    def _metric_data(self, cw, queries, start, end):
        out = {}
        for i in range(0, len(queries), 450):
            kw = dict(MetricDataQueries=queries[i:i + 450],
                      StartTime=datetime.datetime.fromtimestamp(start / 1000, datetime.timezone.utc),
                      EndTime=datetime.datetime.fromtimestamp(end / 1000, datetime.timezone.utc))
            while True:
                r = cw.get_metric_data(**kw)
                for m in r.get("MetricDataResults", []):
                    out.setdefault(m["Id"], []).extend(m.get("Values", []))
                if not r.get("NextToken"):
                    break
                kw["NextToken"] = r["NextToken"]
        return out

    def api_metrics(self, cw, apis, now):
        q, ids = [], {}
        for ai, a in enumerate(apis):
            a["metrics"] = {}
            for si, st in enumerate(a["stages"][:10]):
                if a["kind"] == "REST":
                    dims = [{"Name": "ApiName", "Value": a["name"]}, {"Name": "Stage", "Value": st}]
                    names = {"count": "Count", "e4": "4XXError", "e5": "5XXError", "lat": "Latency"}
                else:
                    dims = [{"Name": "ApiId", "Value": a["id"]}, {"Name": "Stage", "Value": st}]
                    names = {"count": "Count", "e4": "4xx", "e5": "5xx", "lat": "Latency"}
                for k, mn in names.items():
                    qid = f"a{ai}s{si}{k}"
                    ids[qid] = (ai, st, k)
                    q.append({"Id": qid, "ReturnData": True, "MetricStat": {
                        "Metric": {"Namespace": "AWS/ApiGateway", "MetricName": mn, "Dimensions": dims},
                        "Period": 3600, "Stat": "p99" if k == "lat" else "Sum"}})
        res = self._metric_data(cw, q, now - DAY_MS, now)
        for qid, (ai, st, k) in ids.items():
            vals = res.get(qid, [])
            m = apis[ai]["metrics"].setdefault(st, {})
            m[k] = (max(vals) if vals else None) if k == "lat" else sum(vals)

    def lambda_metrics(self, cw, functions, now):
        q, ids = [], {}
        for i, f in enumerate(functions):
            for k, mn, stat in (("thr", "Throttles", "Sum"), ("conc", "ConcurrentExecutions", "Maximum"),
                                ("err", "Errors", "Sum"), ("inv", "Invocations", "Sum")):
                qid = f"f{i}{k}"
                ids[qid] = (i, k)
                q.append({"Id": qid, "ReturnData": True, "MetricStat": {
                    "Metric": {"Namespace": "AWS/Lambda", "MetricName": mn,
                               "Dimensions": [{"Name": "FunctionName", "Value": f["name"]}]},
                    "Period": 86400, "Stat": stat}})
        res = self._metric_data(cw, q, now - DAY_MS, now)
        for qid, (i, k) in ids.items():
            vals = res.get(qid, [])
            functions[i].setdefault("metrics", {})[k] = (max(vals) if k == "conc" else sum(vals)) if vals else 0


# --------------------------------------------------------------------------- analyses
PRICE_GBS = {"x86_64": 0.0000166667, "arm64": 0.0000133334}
PRICE_REQ = 0.20 / 1_000_000


def function_health(snap, stats, now_ms):
    rows = []
    for f in snap.get("functions", []):
        st = stats.get(f["log_group"], {})
        rs, rtext = runtime_status(f["runtime"], now_ms)
        flags = []
        if rs == "deprecated":
            flags.append("runtime deprecated")
        elif rs == "soon":
            flags.append("runtime deprecating soon")
        outdated = []
        for arn in f["layers"]:
            base, ver = arn.rsplit(":", 1)
            latest = (snap.get("layers", {}).get(base) or {}).get("latest")
            if latest and int(ver) < latest:
                outdated.append(f"{base.split(':')[-1]}:{ver} (latest {latest})")
        if outdated:
            flags.append("outdated layer")
        n = st.get("n", 0)
        tmo_pct = 100 * st.get("dur_max", 0) / (f["timeout"] * 1000) if n else None
        mem_pct = 100 * st.get("mem_max", 0) / f["memory"] if n and f["memory"] else None
        if st.get("timeouts"):
            flags.append(f"timed out {st['timeouts']}x")
        elif tmo_pct and tmo_pct >= 80:
            flags.append(f"near timeout ({tmo_pct:.0f}%)")
        if mem_pct and mem_pct >= 85:
            flags.append(f"memory {mem_pct:.0f}% used")
        met = f.get("metrics") or {}
        if met.get("thr"):
            flags.append(f"throttled {int(met['thr'])}x")
        gbs = st.get("billed_ms", 0) / 1000 * f["memory"] / 1024
        cost = gbs * PRICE_GBS.get(f["arch"], PRICE_GBS["x86_64"]) + n * PRICE_REQ
        rows.append({**{k: f[k] for k in ("name", "runtime", "timeout", "memory", "arch", "layers", "secrets", "log_group")},
                     "runtime_status": rs, "runtime_text": rtext, "outdated_layers": outdated,
                     "invocations_24h": n if n else int(met.get("inv") or 0), "from_logs": bool(n),
                     "avg_ms": round(st["dur_sum"] / n) if n else None, "max_ms": round(st.get("dur_max", 0)) if n else None,
                     "timeout_pct": round(tmo_pct) if tmo_pct is not None else None,
                     "mem_used_mb": st.get("mem_max") if n else None, "mem_pct": round(mem_pct) if mem_pct is not None else None,
                     "cold_starts": st.get("cold_starts", 0), "throttles": int(met.get("thr") or 0),
                     "max_concurrency": int(met.get("conc") or 0),
                     "cost_24h": round(cost, 4), "cost_30d": round(cost * 30, 2), "flags": flags})
    rows.sort(key=lambda r: (-len(r["flags"]), r["name"]))
    return rows


def schedule_health(snap, stats, now_ms, data_from_ms, store=None, profile=None):
    """Expected runs (from the schedule) vs actual runs (Lambda REPORT lines / SFN executions)."""
    fn_names = {f["name"]: f for f in snap.get("functions", [])}
    start = max(now_ms - DAY_MS, data_from_ms)
    out = []
    for sc in snap.get("schedules", []):
        kind, val = parse_schedule(sc["expression"])
        r = {**{k: sc.get(k) for k in ("source", "name", "group", "expression", "tz", "state", "function",
                                       "state_machine", "target", "dlq", "retries")},
             "next_run": None, "expected": None, "actual": None, "missed": [], "last_run": None,
             "last_run_status": None, "status": "ok", "detail": ""}
        if kind:
            r["next_run"] = next_run(kind, val, sc["tz"], now_ms, sc.get("created"))
        if sc["tz"] not in ("UTC", None) and not _tz(sc["tz"])[1]:
            r["detail"] += "time zone database missing (pip install tzdata) - times treated as UTC. "
        if sc["state"] == "DISABLED":
            r["status"], r["detail"] = "disabled", r["detail"] + "Schedule is turned off."
            out.append(r)
            continue
        f = fn_names.get(sc["function"]) if sc["function"] else None
        if sc["function"] and not f and snap.get("functions"):
            r["status"], r["detail"] = "target missing", f"Target Lambda {sc['function']} doesn't exist."
            out.append(r)
            continue
        # actual runs
        if f:
            st = stats.get(f["log_group"], {})
            runs = [ts for ts, _ in st.get("runs", [])]
            r["last_status_raw"] = st.get("last_status")
            tol = (f["timeout"] + 60) * 1000          # REPORT is written when the run ends
        elif sc["state_machine"] and "sfn_starts" in sc:
            runs, tol = sc["sfn_starts"], 5 * 60_000
        else:
            r["status"], r["detail"] = "not checked", r["detail"] + (sc.get("sfn_error") or "Target isn't a Lambda or state machine.")
            out.append(r)
            continue
        runs = [t for t in runs if t >= start - tol]
        if runs:
            r["last_run"] = runs[-1]
        if f and stats.get(f["log_group"], {}).get("last_status") in ("timeout", "error"):
            r["last_run_status"] = stats[f["log_group"]]["last_status"]
        if not kind:
            r["status"], r["detail"] = "not checked", r["detail"] + val
            out.append(r)
            continue
        if now_ms - start < 30 * 60_000:
            r["status"], r["detail"] = "not enough data", r["detail"] + "Less than 30 min of logs loaded."
            out.append(r)
            continue
        flex = (sc.get("flex_minutes") or 0) * 60_000
        if kind == "rate":
            exp = int((now_ms - start) / (val * 1000))
            r["expected"], r["actual"] = exp, len([t for t in runs if t >= start])
            gap = now_ms - (runs[-1] if runs else start)
            if exp >= 2 and gap > 2.5 * val * 1000 + tol:
                r["status"] = "not running"
                r["detail"] += (f"No run for {gap / HOUR_MS:.1f}h (expected every {val // 60} min). "
                                f"Expected ~{exp} runs in the window, saw {r['actual']}.")
            elif exp >= 2 and r["actual"] < 0.8 * exp:
                r["status"] = "missed runs" if r["actual"] else "not running"
                r["detail"] += f"Expected ~{exp} runs, saw {r['actual']}."
        else:
            times = cron_times(val, start, now_ms - tol - flex, sc["tz"]) if kind == "cron" else \
                [t for t in [next_run("at", val, sc["tz"], start - 1)] if t and t < now_ms - tol]
            r["expected"] = len(times)
            if len(times) > 300:              # very frequent: compare counts
                r["actual"] = len([t for t in runs if t >= start])
                if r["actual"] < 0.8 * len(times):
                    r["status"] = "missed runs" if r["actual"] else "not running"
                    r["detail"] += f"Expected ~{len(times)} runs, saw {r['actual']}."
            else:
                import bisect
                missed = []
                for t in times:
                    i = bisect.bisect_left(runs, t - 120_000)
                    if not (i < len(runs) and runs[i] <= t + tol + flex):
                        missed.append(t)
                r["actual"] = len(times) - len(missed)
                r["missed"] = missed[-20:]
                if missed:
                    r["status"] = "not running" if len(missed) == len(times) else "missed runs"
                    r["detail"] += f"{len(missed)} of {len(times)} expected runs didn't happen."
        if r["status"] == "ok" and r["last_run_status"] in ("timeout", "error"):
            r["status"], r["detail"] = "last run failed", r["detail"] + f"Last run ended with status {r['last_run_status']}."
        out.append(r)
    order = {"not running": 0, "missed runs": 1, "target missing": 2, "last run failed": 3, "disabled": 4,
             "not enough data": 5, "not checked": 6, "ok": 7}
    out.sort(key=lambda r: (order.get(r["status"], 9), r["name"]))
    return out


def secrets_report(snap, now_ms, auth_problems=None):
    """Secrets metadata with flags; links to auth errors that started soon after a change."""
    rows = []
    for s in snap.get("secrets", []):
        flags = []
        if s.get("last_changed") and now_ms - s["last_changed"] < 2 * DAY_MS:
            flags.append("changed in the last 48h")
        if s.get("last_accessed") and now_ms - s["last_accessed"] > 30 * DAY_MS:
            flags.append(f"not read in {int((now_ms - s['last_accessed']) / DAY_MS)} days")
        if s.get("rotation") and s.get("next_rotation") and s["next_rotation"] < now_ms:
            flags.append("rotation overdue")
        related = []
        for p in auth_problems or []:
            if s.get("last_changed") and 0 <= p["first"] - s["last_changed"] <= 2 * DAY_MS and \
                    (not s["functions"] or set(s["functions"]) & {g.split("/")[-1] for g in p["functions"]}):
                related.append(f"{p['service']} {p['problem']} errors began "
                               f"{(p['first'] - s['last_changed']) / HOUR_MS:.1f}h after this changed")
        if related:
            flags.append("possibly related to auth errors")
        rows.append({**s, "flags": flags, "related": related})
    rows.sort(key=lambda r: (not r["related"], -len(r["flags"]), r["name"]))
    return rows


def api_report(snap):
    rows = []
    for a in snap.get("apis", []):
        for st in a["stages"] or ["-"]:
            m = (a.get("metrics") or {}).get(st, {})
            cnt, e5, e4 = m.get("count") or 0, m.get("e5") or 0, m.get("e4") or 0
            flags = []
            if e5:
                flags.append(f"{int(e5)} server errors (5xx)")
            if cnt and e4 / cnt > 0.2:
                flags.append(f"{100 * e4 / cnt:.0f}% client errors (4xx)")
            if m.get("lat") and m["lat"] > 10_000:
                flags.append(f"slow (p99 {m['lat'] / 1000:.1f}s)")
            rows.append({"api": a["name"], "kind": a["kind"], "id": a["id"], "stage": st, "requests": int(cnt),
                         "e4": int(e4), "e5": int(e5), "p99_ms": round(m["lat"]) if m.get("lat") else None,
                         "routes": a["routes"], "flags": flags})
    rows.sort(key=lambda r: (-r["e5"], -len(r["flags"]), -r["requests"]))
    return rows


def layers_report(snap, store):
    rows = []
    for base, info in (snap.get("layers") or {}).items():
        for ver, fns in sorted(info["used_versions"].items(), key=lambda x: -int(x[0])):
            pk = store.get_layer_pkgs(f"{base}:{ver}") or {}
            rows.append({"layer": info["name"], "arn": base, "version": int(ver), "latest": info["latest"],
                         "outdated": bool(info["latest"] and int(ver) < info["latest"]),
                         "functions": fns, "packages": {k: v for k, v in pk.items() if not k.startswith("_")},
                         "note": pk.get("_note") or info.get("error")})
    return rows
