#!/usr/bin/env python3
"""Logan Runway Watch.

Tells you which runways Boston Logan (KBOS) is using right now, and alerts you
when the runways that send planes over your house switch on or off.

The source of truth is Logan's digital ATIS: the recorded broadcast where the
tower announces the landing and departing runways. Wind is shown alongside so
you can see how often the tower's choice does *not* simply follow the wind.

No third-party packages needed. Python 3.8+.

    python3 logan_runways.py                       # what's active now
    python3 logan_runways.py watch -r arr:4R -r dep:27 --ntfy my-secret-topic
    python3 logan_runways.py mark                  # "planes overhead right now"
    python3 logan_runways.py stats                 # what the log shows so far
    python3 logan_runways.py parse "ATIS TEXT"     # test the parser
"""

import argparse
import csv
import json
import math
import os
import platform
import re
import shutil
import subprocess
import sys
import time
import urllib.request
from collections import Counter
from datetime import datetime, timedelta, timezone

DATIS_URL = "https://datis.clowd.io/api/KBOS"
METAR_URL = "https://aviationweather.gov/api/data/metar?ids=KBOS&format=json"
USER_AGENT = "logan-runway-watch/1.0"

# Magnetic north at Boston is about 14 degrees west of true north. Runway
# numbers and ATIS winds are magnetic; METAR winds are true.
MAG_VAR = 14

RUNWAYS = ["4L", "4R", "9", "14", "15L", "15R", "22L", "22R", "27", "32", "33L", "33R"]

HERE = os.path.dirname(os.path.abspath(__file__))
DEFAULT_LOG = os.path.join(HERE, "runway_log.csv")
DEFAULT_MARKS = os.path.join(HERE, "overhead_marks.csv")
DEFAULT_STATE = os.path.join(HERE, ".runway_state.json")

LOG_FIELDS = ["time_utc", "atis", "landing", "departing", "wind_dir_mag", "wind_kt", "gust_kt", "watched_active"]


# ---------------------------------------------------------------- parsing

RWY_WORDS = {"RWY", "RWYS", "RY", "RYS", "RUNWAY", "RUNWAYS"}
CONNECTORS = {",", "AND", "&"}
RWY_ID = re.compile(r"^0?([1-9]|[12][0-9]|3[0-6])([LRC]?)$")

# Clauses about closures, taxiing or hold-short points mention runways that
# are not "in use" in the sense we care about.
SKIP_PHRASES = ("CLSD", "CLOSED", "HOLD SHORT", "TWY", "TAXIWAY", "OUT OF SERVICE",
                " OTS", "U/S", "CONSTRUCTION", "RVR", "FICON", "BRAKING", "NOTAM")

# Strong keywords say outright whether a runway is for landing or departing.
# Weak ones (approach types) only count when nothing stronger is nearby.
WEAK_ARR = {"ILS", "LOC", "RNAV", "RNP", "VISUAL", "GPS", "VOR", "LDA", "SDF", "SIMUL"}


def _keyword(token):
    if token.startswith("DEP") or token in {"TKOF", "TKOFS", "TAKEOFF", "TAKEOFFS"}:
        return ("dep", True)
    if token.startswith(("APCH", "APP", "LDG", "LNDG", "LAND", "ARR", "ARVL")):
        return ("arr", True)
    if token in WEAK_ARR:
        return ("arr", False)
    return None


def _norm_runway(token):
    m = RWY_ID.match(token)
    return m.group(1) + m.group(2) if m else None


def parse_atis(text, default=None):
    """Return (landing, departing) runway sets announced in an ATIS text.

    default is "arr" or "dep" for an arrival-only or departure-only ATIS: a
    runway mentioned without any landing/departing keyword is given that use.
    """
    landing, departing = set(), set()
    text = text.upper().replace(",", " , ")
    for clause in re.split(r"[.;](?:\s|$)", text):
        if any(p in clause for p in SKIP_PHRASES):
            continue
        tokens = re.findall(r"[A-Z0-9/]+|,|&", clause)
        keywords = [(i, *kw) for i, t in enumerate(tokens) if (kw := _keyword(t))]

        i = 0
        while i < len(tokens):
            if tokens[i] not in RWY_WORDS:
                i += 1
                continue
            group, j = [], i + 1
            while j < len(tokens) and (tokens[j] in CONNECTORS or _norm_runway(tokens[j])):
                if tokens[j] not in CONNECTORS:
                    group.append(_norm_runway(tokens[j]))
                j += 1
            if group:
                use = _use_for_group(keywords, i, j) or default
                if use == "arr":
                    landing.update(group)
                elif use == "dep":
                    departing.update(group)
            i = j
    return landing, departing


def _use_for_group(keywords, start, end):
    before = [k for k in keywords if k[0] < start]
    if before:
        return max(before)[1]  # nearest keyword before the runway list
    after = [k for k in keywords if k[0] >= end]
    strong = [k for k in after if k[2]]
    pick = strong or after
    return min(pick)[1] if pick else None


WIND_RE = re.compile(r"\b(\d{3}|VRB)(\d{2,3})(?:G(\d{2,3}))?KT\b")


def parse_wind(text):
    """Return (direction or None for variable, speed, gust or None) from a wind group."""
    m = WIND_RE.search(text.upper())
    if not m:
        return None
    direction = None if m.group(1) == "VRB" else int(m.group(1))
    return direction, int(m.group(2)), int(m.group(3)) if m.group(3) else None


def runway_heading(rwy):
    return int(re.match(r"\d+", rwy).group()) * 10


def wind_components(wind_dir, wind_kt, rwy):
    """(headwind, crosswind) in knots. Negative headwind means tailwind."""
    a = math.radians(wind_dir - runway_heading(rwy))
    return wind_kt * math.cos(a), abs(wind_kt * math.sin(a))


# ---------------------------------------------------------------- fetching

def http_get(url, timeout=20):
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read().decode("utf-8", "replace")


def fetch_status():
    data = json.loads(http_get(DATIS_URL))
    if isinstance(data, dict):
        if "error" in data:
            raise RuntimeError("D-ATIS service: " + str(data["error"]))
        data = [data]

    landing, departing, codes, texts = set(), set(), [], []
    for item in data:
        text = item.get("datis") or ""
        kind = (item.get("type") or "").lower()
        default = kind if kind in ("arr", "dep") else None
        arr, dep = parse_atis(text, default)
        landing |= arr
        departing |= dep
        if item.get("code"):
            codes.append(item["code"])
        texts.append(text)

    wind = None
    for text in texts:
        wind = parse_wind(text)
        if wind:
            break
    wind_source = "ATIS"
    if not wind:
        wind, wind_source = fetch_metar_wind(), "METAR"

    return {
        "time": datetime.now(timezone.utc),
        "atis": "/".join(codes),
        "landing": landing,
        "departing": departing,
        "wind": wind,
        "wind_source": wind_source,
        "texts": texts,
    }


def fetch_metar_wind():
    try:
        obs = json.loads(http_get(METAR_URL))
        wind = parse_wind(obs[0].get("rawOb", ""))
    except Exception:
        return None
    if wind and wind[0] is not None:
        wind = ((wind[0] + MAG_VAR) % 360, wind[1], wind[2])
    return wind


# ---------------------------------------------------------------- watching

def parse_watch(specs):
    """'arr:4R', 'dep:27' or plain '4R' (either use) -> set of (use, runway)."""
    watched = set()
    for spec in specs:
        use, _, rwy = spec.upper().rpartition(":")
        rwy = _norm_runway(rwy)
        if not rwy:
            sys.exit(f"Unknown runway in {spec!r}. Logan's runways: {', '.join(RUNWAYS)}")
        uses = {"ARR": ["arr"], "DEP": ["dep"], "": ["arr", "dep"]}.get(use)
        if uses is None:
            sys.exit(f"Use arr:RUNWAY, dep:RUNWAY or just RUNWAY, not {spec!r}")
        watched.update((u, rwy) for u in uses)
    return watched


def active_uses(status):
    return {("arr", r) for r in status["landing"]} | {("dep", r) for r in status["departing"]}


def _rwy_order(r):
    return RUNWAYS.index(r) if r in RUNWAYS else len(RUNWAYS)


def sort_rwys(rwys):
    return sorted(rwys, key=_rwy_order)


def describe_uses(uses):
    names = {"arr": "landing", "dep": "departing"}
    return ", ".join(f"{names[u]} {r}" for u, r in sorted(uses, key=lambda x: (x[0], _rwy_order(x[1]))))


def fmt_rwys(rwys):
    return ", ".join(sort_rwys(rwys)) or "none announced"


def print_status(status, watched=None, verbose=False):
    local = status["time"].astimezone().strftime("%a %H:%M")
    print(f"\nLogan (KBOS) at {local}   ATIS {status['atis'] or '?'}")
    print(f"  Landing:   {fmt_rwys(status['landing'])}")
    print(f"  Departing: {fmt_rwys(status['departing'])}")

    wind = status["wind"]
    if wind:
        d, kt, gust = wind
        g = f" gusting {gust}" if gust else ""
        print(f"  Wind:      {'variable' if d is None else f'{d:03d}° magnetic'} at {kt} kt{g} ({status['wind_source']})")
        if d is not None and kt > 0:
            parts = []
            for r in sort_rwys(status["landing"] | status["departing"]):
                head, _ = wind_components(d, kt, r)
                parts.append(f"{r} {'headwind' if head >= 0 else 'tailwind'} {abs(head):.0f}")
            if parts:
                print(f"             on the active runways: {'; '.join(parts)} kt")
        if kt < 8 or d is None:
            print("             Light wind: almost any runway works, so the tower usually keeps its current setup.")

    if watched:
        hits = watched & active_uses(status)
        if hits:
            print(f"  >>> YOUR RUNWAYS ARE ACTIVE ({describe_uses(hits)}). Expect planes over the house.")
        else:
            print(f"  Your runways ({describe_uses(watched)}) are quiet.")

    if verbose:
        for t in status["texts"]:
            print("\n  ATIS text: " + t)


def append_log(path, status, watched_active):
    new = not os.path.exists(path)
    wind = status["wind"] or (None, None, None)
    with open(path, "a", newline="") as f:
        w = csv.writer(f)
        if new:
            w.writerow(LOG_FIELDS)
        w.writerow([
            status["time"].strftime("%Y-%m-%dT%H:%MZ"),
            status["atis"],
            " ".join(sort_rwys(status["landing"])),
            " ".join(sort_rwys(status["departing"])),
            "" if wind[0] is None else wind[0],
            "" if wind[1] is None else wind[1],
            "" if wind[2] is None else wind[2],
            "" if watched_active is None else int(watched_active),
        ])


def load_state(path):
    try:
        with open(path) as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def save_state(path, state):
    try:
        with open(path, "w") as f:
            json.dump(state, f)
    except OSError as e:
        print(f"  (could not save state: {e})")


# ---------------------------------------------------------------- notifying

def notify(title, message, ntfy_topic=None, desktop=True, urgent=False):
    print(f"  NOTIFY: {title}: {message}")
    if desktop:
        try:
            desktop_notify(title, message)
        except Exception as e:
            print(f"  (desktop notification failed: {e})")
    if ntfy_topic:
        try:
            req = urllib.request.Request(
                "https://ntfy.sh/" + ntfy_topic,
                data=message.encode("utf-8"),
                headers={"Title": title, "Tags": "airplane", "Priority": "high" if urgent else "default",
                         "User-Agent": USER_AGENT},
            )
            urllib.request.urlopen(req, timeout=20).read()
        except Exception as e:
            print(f"  (phone notification via ntfy failed: {e})")


def desktop_notify(title, message):
    system = platform.system()
    if system == "Darwin":
        script = f"display notification {json.dumps(message)} with title {json.dumps(title)} sound name \"Glass\""
        subprocess.run(["osascript", "-e", script], check=False)
    elif system == "Linux" and shutil.which("notify-send"):
        subprocess.run(["notify-send", title, message], check=False)
    elif system == "Windows":
        ps = ("Add-Type -AssemblyName System.Windows.Forms; Add-Type -AssemblyName System.Drawing; "
              "$n = New-Object System.Windows.Forms.NotifyIcon; $n.Icon = [System.Drawing.SystemIcons]::Information; "
              "$n.Visible = $true; $n.ShowBalloonTip(10000, $env:LRW_TITLE, $env:LRW_MSG, 'Info'); "
              "Start-Sleep 11; $n.Dispose()")
        env = dict(os.environ, LRW_TITLE=title, LRW_MSG=message)
        subprocess.Popen(["powershell", "-NoProfile", "-Command", ps], env=env)


# ---------------------------------------------------------------- commands

def check_once(args, watched, state):
    """Fetch, print, log and notify on change. Returns the updated state."""
    status = fetch_status()
    print_status(status, watched, args.verbose)

    hits = watched & active_uses(status) if watched else set()
    is_active = bool(hits)
    append_log(args.log, status, is_active if watched else None)

    if watched and state.get("active") != is_active:
        if is_active:
            notify("Planes over your house", f"Logan switched to {describe_uses(hits)}.",
                   args.ntfy, not args.no_desktop, urgent=True)
        elif "active" in state:
            notify("Quiet again", f"Logan stopped using {describe_uses(watched)}. "
                   f"Now landing {fmt_rwys(status['landing'])}, departing {fmt_rwys(status['departing'])}.",
                   args.ntfy, not args.no_desktop)
    return {"active": is_active}


def cmd_now(args):
    watched = parse_watch(args.runway)
    try:
        status = fetch_status()
    except Exception as e:
        sys.exit(f"Could not read Logan's ATIS: {e}")
    print_status(status, watched, args.verbose)
    append_log(args.log, status, bool(watched & active_uses(status)) if watched else None)


def cmd_watch(args):
    watched = parse_watch(args.runway)
    if not watched:
        print("No runways given with -r, so this will log runway use but never alert.\n"
              "Run `mark` a few times when planes are overhead to find out which runways to watch.")
    state = load_state(args.state)
    while True:
        try:
            state = check_once(args, watched, state)
            save_state(args.state, state)
        except Exception as e:
            print(f"\n  Could not read Logan's ATIS ({e}). Trying again next round.")
        if args.once:
            return
        time.sleep(max(60, args.interval * 60))


def cmd_mark(args):
    try:
        status = fetch_status()
    except Exception as e:
        sys.exit(f"Could not read Logan's ATIS: {e}")
    print_status(status)
    append_log(args.log, status, None)
    new = not os.path.exists(args.marks)
    with open(args.marks, "a", newline="") as f:
        w = csv.writer(f)
        if new:
            w.writerow(["time_utc", "landing", "departing"])
        w.writerow([status["time"].strftime("%Y-%m-%dT%H:%MZ"),
                    " ".join(sorted(status["landing"])), " ".join(sorted(status["departing"]))])
    print("\nSaved: planes overhead right now.")
    summarize_marks(args.marks)


def summarize_marks(path):
    with open(path, newline="") as f:
        rows = list(csv.DictReader(f))
    tally = Counter()
    for row in rows:
        tally.update(f"arr:{r}" for r in row["landing"].split())
        tally.update(f"dep:{r}" for r in row["departing"].split())
    n = len(rows)
    print(f"\nAcross your {n} overhead mark{'s' if n != 1 else ''}, runways in use:")
    for spec, count in tally.most_common():
        print(f"  {spec:8} {count}/{n}  {'#' * round(20 * count / n)}")
    if n < 3:
        print("Mark a few more times, ideally on different days, to separate your runway from the others.")
    elif tally:
        top = max(tally.values())
        likely = [s for s, c in tally.items() if c == top]
        when = "every time you marked" if top == n else f"in {top} of your {n} marks"
        print(f"In use {when}: {' '.join(likely)}. Try: watch " + " ".join(f"-r {s}" for s in likely))


def read_log(path):
    if not os.path.exists(path):
        sys.exit(f"No log yet at {path}. Run `watch` for a while first.")
    with open(path, newline="") as f:
        rows = list(csv.DictReader(f))
    if not rows:
        sys.exit("The log is empty.")
    for r in rows:
        r["time"] = datetime.strptime(r["time_utc"], "%Y-%m-%dT%H:%MZ").replace(tzinfo=timezone.utc).astimezone()
    return rows


def setup_label(row):
    return f"land {row['landing'] or '-'} / depart {row['departing'] or '-'}"


def noisy_stretches(rows, max_gap_min=30):
    """Group consecutive checks where your runways were active.

    A gap of more than max_gap_min between checks (computer asleep, network
    down) ends a stretch at the last check we actually saw.
    """
    gap = max_gap_min * 60
    stretches, cur = [], None
    for r in rows:
        flag = r.get("watched_active")
        if flag not in ("0", "1"):
            continue
        t = r["time"]
        if cur and (t - cur["last"]).total_seconds() > gap:
            cur["end"] = cur["last"]
            stretches.append(cur)
            cur = None
        if flag == "1":
            if not cur:
                cur = {"start": t, "setups": []}
            cur["last"] = t
            if setup_label(r) not in cur["setups"]:
                cur["setups"].append(setup_label(r))
        elif cur:
            cur["end"] = t
            stretches.append(cur)
            cur = None
    if cur:
        cur["end"], cur["ongoing"] = cur["last"], True
        stretches.append(cur)
    return stretches


def is_night(t):
    return t.hour >= 22 or t.hour < 7


def night_minutes(start, end):
    t, minutes = start, 0
    while t < end:
        minutes += is_night(t)
        t += timedelta(minutes=1)
    return minutes


def fmt_minutes(m):
    h, m = divmod(int(m), 60)
    return f"{h} h {m:02d} min" if h else f"{m} min"


def cmd_report(args):
    rows = read_log(args.log)
    since = datetime.now().astimezone() - timedelta(days=args.days)
    stretches = [s for s in noisy_stretches(rows) if s["end"] >= since]
    if not any(r.get("watched_active") in ("0", "1") for r in rows):
        sys.exit("The log has no checks with your runways set. Run `watch -r ...` first.")

    print(f"Times Logan used your runways, last {args.days} days (local time)\n")
    if not stretches:
        print("  None. Quiet the whole time.")
        return
    total = night = 0
    for s in stretches:
        start = max(s["start"], since)
        minutes = (s["end"] - start).total_seconds() / 60
        n = night_minutes(start, s["end"])
        total, night = total + minutes, night + n
        end_txt = "now (ongoing)" if s.get("ongoing") else s["end"].strftime("%H:%M")
        if s["end"].date() != start.date() and not s.get("ongoing"):
            end_txt = s["end"].strftime("%a %H:%M")
        length = fmt_minutes(minutes) if minutes else "single check"
        overnight = f"   ({fmt_minutes(n)} between 10 pm and 7 am)" if n else ""
        print(f"  {start:%a %b %d  %H:%M} to {end_txt:<13} {length:>14}   {'; '.join(s['setups'])}{overnight}")
    print(f"\nTotal: {fmt_minutes(total)} over {len(stretches)} stretch{'es' if len(stretches) != 1 else ''}, "
          f"{fmt_minutes(night)} of it between 10 pm and 7 am.")
    print("\nTo complain, contact Massport's Noise Abatement Office: call (617) 561-3333, or use the noise\n"
          "complaint form on massport.com (Community, then Noise Abatement). Give your address and these times.")


def cmd_stats(args):
    rows = read_log(args.log)
    n = len(rows)
    configs = Counter(setup_label(r) for r in rows)
    print(f"{n} checks from {rows[0]['time_utc']} to {rows[-1]['time_utc']}\n")
    print("Runway setups seen (share of checks):")
    for cfg, c in configs.most_common(10):
        print(f"  {100 * c / n:5.1f}%  {cfg}")

    changes = sum(1 for a, b in zip(rows, rows[1:]) if (a["landing"], a["departing"]) != (b["landing"], b["departing"]))
    print(f"\nThe setup changed {changes} time{'s' if changes != 1 else ''}.")

    flagged = [r for r in rows if r.get("watched_active") in ("0", "1")]
    if flagged:
        share = 100 * sum(r["watched_active"] == "1" for r in flagged) / len(flagged)
        print(f"Your runways were active in {share:.0f}% of checks.")
        print("\nBy hour of day (share of checks your runways were active):")
        for hour in range(24):
            at = [r for r in flagged if r["time"].hour == hour]
            if not at:
                continue
            pct = 100 * sum(r["watched_active"] == "1" for r in at) / len(at)
            print(f"  {hour:02d}:00  {pct:3.0f}%  {'#' * round(pct / 5)}")

    light = [r for r in rows if r["wind_kt"] and int(r["wind_kt"]) < 8]
    if light:
        print(f"Wind was under 8 kt in {100 * len(light) / n:.0f}% of checks. "
              "In light wind the tower is free to keep whatever setup it likes.")


def cmd_parse(args):
    landing, departing = parse_atis(args.text, args.default)
    print(f"Landing:   {fmt_rwys(landing)}\nDeparting: {fmt_rwys(departing)}")
    wind = parse_wind(args.text)
    if wind:
        print(f"Wind:      {wind}")


def main(argv=None):
    p = argparse.ArgumentParser(description="See which runways Boston Logan is using and get alerts for yours.")
    p.add_argument("--log", default=DEFAULT_LOG, help="CSV file every check is appended to")
    p.add_argument("-v", "--verbose", action="store_true", help="also print the raw ATIS text")
    sub = p.add_subparsers(dest="cmd")

    def add_runway_arg(sp):
        sp.add_argument("-r", "--runway", action="append", default=[], metavar="USE:RWY",
                        help="a runway that brings planes over you, e.g. arr:4R, dep:27, or 33L for both. Repeatable.")

    now = sub.add_parser("now", help="show what's active right now (default)")
    add_runway_arg(now)

    watch = sub.add_parser("watch", help="keep checking and alert when your runways turn on or off")
    add_runway_arg(watch)
    watch.add_argument("-i", "--interval", type=float, default=5, help="minutes between checks (default 5)")
    watch.add_argument("--ntfy", metavar="TOPIC", help="also push to your phone through the ntfy app on this topic")
    watch.add_argument("--no-desktop", action="store_true", help="skip desktop notifications")
    watch.add_argument("--once", action="store_true", help="check one time and exit (for cron or Task Scheduler)")
    watch.add_argument("--state", default=DEFAULT_STATE, help=argparse.SUPPRESS)

    mark = sub.add_parser("mark", help="record that planes are over your house right now")
    mark.add_argument("--marks", default=DEFAULT_MARKS, help="CSV file for your marks")

    sub.add_parser("stats", help="summarize the log, including which hours are usually quiet")

    report = sub.add_parser("report", help="list when your runways were active, ready for a noise complaint")
    report.add_argument("-d", "--days", type=int, default=7, help="how many days back (default 7)")

    parse = sub.add_parser("parse", help="run the ATIS parser on some text")
    parse.add_argument("text")
    parse.add_argument("--default", choices=["arr", "dep"], help="treat as an arrival-only or departure-only ATIS")

    args = p.parse_args(argv)
    if args.cmd is None:
        args.cmd, args.runway = "now", []
    {"now": cmd_now, "watch": cmd_watch, "mark": cmd_mark, "stats": cmd_stats, "report": cmd_report,
     "parse": cmd_parse}[args.cmd](args)


if __name__ == "__main__":
    main()
