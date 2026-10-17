#!/usr/bin/env python3
"""Scheduled job: read Logan's ATIS, update the phone app's data, alert on change.

Runs every 10 minutes in GitHub Actions. Writes site/status.json (right now)
and site/history.json (every check, last 45 days), which the page reads.

Settings come from the environment:
    WATCH       runways that bring planes over the house, e.g. "33L 27" or
                "dep:33L arr:27" (default "33L 27": both, landing and departing)
    NTFY_TOPIC  ntfy topic to push alerts to (optional, keep it secret)
"""

import json
import os
from datetime import datetime, timedelta, timezone

import logan_runways as lr

SITE = os.path.join(lr.HERE, "site")
STATUS = os.path.join(SITE, "status.json")
HISTORY = os.path.join(SITE, "history.json")
KEEP_DAYS = 45
TIME_FMT = "%Y-%m-%dT%H:%MZ"


def load(path, default):
    try:
        with open(path) as f:
            return json.load(f)
    except (OSError, ValueError):
        return default


def save(path, data):
    with open(path, "w") as f:
        json.dump(data, f, separators=(",", ":"))


def main():
    os.makedirs(SITE, exist_ok=True)
    watched = lr.parse_watch((os.environ.get("WATCH") or "33L 27").replace(",", " ").split())
    topic = os.environ.get("NTFY_TOPIC") or None
    prev = load(STATUS, {})
    now = datetime.now(timezone.utc)

    try:
        status = lr.fetch_status()
    except Exception as e:
        # Keep the last good reading so the page still shows something, and say why it's old.
        print(f"Could not read Logan's ATIS: {e}")
        prev.update(error=str(e), error_time=now.strftime(TIME_FMT))
        save(STATUS, prev)
        return

    lr.print_status(status, watched, verbose=True)
    hits = watched & lr.active_uses(status)
    wind = status["wind"]
    out = {
        "time": status["time"].strftime(TIME_FMT),
        "atis": status["atis"],
        "landing": lr.sort_rwys(status["landing"]),
        "departing": lr.sort_rwys(status["departing"]),
        "wind": {"dir": wind[0], "kt": wind[1], "gust": wind[2], "source": status["wind_source"]} if wind else None,
        "texts": status["texts"],
        "watched": sorted(f"{u}:{r}" for u, r in watched),
        "watched_active": bool(hits),
    }
    save(STATUS, out)

    history = load(HISTORY, {"fields": ["t", "atis", "landing", "departing", "wind_dir", "wind_kt", "gust_kt"], "rows": []})
    history["rows"].append([out["time"], out["atis"], " ".join(out["landing"]), " ".join(out["departing"]),
                            *(wind if wind else (None, None, None))])
    cutoff = (now - timedelta(days=KEEP_DAYS)).strftime(TIME_FMT)
    history["rows"] = [r for r in history["rows"] if r[0] >= cutoff]
    save(HISTORY, history)

    if prev.get("watched_active") != out["watched_active"] and topic:
        if hits:
            lr.notify("Planes over your house", f"Logan switched to {lr.describe_uses(hits)}.",
                      topic, desktop=False, urgent=True)
        elif "watched_active" in prev:
            lr.notify("Quiet again", f"Now landing {lr.fmt_rwys(status['landing'])}, "
                      f"departing {lr.fmt_rwys(status['departing'])}.", topic, desktop=False)

    if os.environ.get("TEST_ALERT") == "true":
        if topic:
            state = "Planes over the house" if hits else "Quiet over the house"
            lr.notify("Test alert", f"Logan Runway Watch is connected. Right now: {state}. "
                      f"Landing {lr.fmt_rwys(status['landing'])}, departing {lr.fmt_rwys(status['departing'])}.",
                      topic, desktop=False)
        else:
            print("Test alert skipped: the NTFY_TOPIC secret is not set.")


if __name__ == "__main__":
    main()
