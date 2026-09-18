#!/usr/bin/env python3
"""cost-coach stage 1 — pick candidate sessions and forecast what reading them costs.

Metadata only. Reads no conversation text. This stage is free; stage 2
(extract.py) is what actually spends money, so this output is the basis for
the consent prompt.
"""

import argparse
import collections
import datetime as dt
import glob
import json
import os
import re
import sys

HOME = os.path.expanduser("~")
CLAUDE_DIR = os.path.join(HOME, ".claude")
INSPECTOR_FINDINGS = os.path.join(CLAUDE_DIR, "cost-inspector", "last-audit.json")
KNOWN_SCHEMA_VERSIONS = (1,)
REFERRAL_MAX_AGE_DAYS = 14

# Review runs on a cheap model deliberately: lens detection does not need the
# most capable one, and the user's default is often the expensive one.
REVIEW_MODEL = "claude-sonnet-5"
REVIEW_INPUT_RATE = 2.0  # $/MTok for REVIEW_MODEL

DEFAULT_SELECT_DAYS = 30
DEFAULT_SELECT_SESSIONS = 100
DEFAULT_READ_COUNT = 5


def parse_ts(value):
    try:
        return dt.datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None


def iter_json(path):
    """Yield JSON objects. Non-JSON lines are discarded unread — mixed-format
    files carry conversation text this stage must not look at."""
    try:
        fh = open(path, encoding="utf-8", errors="replace")
    except OSError:
        return
    with fh:
        for raw in fh:
            raw = raw.strip()
            if not raw or raw[0] not in "{[":
                continue
            try:
                yield json.loads(raw)
            except ValueError:
                continue


ACTIVE_WINDOW_MINUTES = 15


def scan_sessions(select_days, max_sessions, include_active=False):
    """One row per session: peak context, turns, models, last activity.

    Sessions touched in the last ACTIVE_WINDOW_MINUTES are skipped by default:
    one of them is almost certainly the session running this review, and
    reviewing your own in-flight session is both a conflict of interest and an
    incomplete read."""
    cutoff = dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=select_days)
    active_cutoff = dt.datetime.now().timestamp() - ACTIVE_WINDOW_MINUTES * 60
    rows = []
    skipped_active = []
    seen = set()
    paths = []
    for p in glob.glob(os.path.join(CLAUDE_DIR, "projects", "*", "*.jsonl")):
        real = os.path.realpath(p)
        if real in seen:
            continue
        seen.add(real)
        if not include_active:
            try:
                if os.path.getmtime(p) > active_cutoff:
                    skipped_active.append(os.path.basename(p).rsplit(".", 1)[0])
                    continue
            except OSError:
                pass
        paths.append(p)

    for path in paths:
        peak = turns = 0
        models = collections.Counter()
        tools = collections.Counter()
        last = None
        for entry in iter_json(path):
            if not isinstance(entry, dict):
                continue
            ts = parse_ts(entry.get("timestamp"))
            if ts and (last is None or ts > last):
                last = ts
            msg = entry.get("message")
            if not isinstance(msg, dict):
                continue
            usage = msg.get("usage")
            if isinstance(usage, dict):
                turns += 1
                models[msg.get("model") or "unknown"] += 1
                ctx = ((usage.get("input_tokens") or 0)
                       + (usage.get("cache_read_input_tokens") or 0))
                peak = max(peak, ctx)
            content = msg.get("content")
            if isinstance(content, list):
                for block in content:
                    if isinstance(block, dict) and block.get("type") == "tool_use":
                        tools[block.get("name") or "?"] += 1
        if not turns or last is None or last < cutoff:
            continue
        rows.append({
            "path": path,
            "session_id": os.path.basename(path).rsplit(".", 1)[0],
            "project": os.path.basename(os.path.dirname(path)),
            "last_active": last.isoformat(),
            "turns": turns,
            "peak_context_tokens": peak,
            "models": dict(models),
            "top_tools": dict(tools.most_common(5)),
            "read_cost_usd": round(peak / 1e6 * REVIEW_INPUT_RATE, 2),
        })

    rows.sort(key=lambda r: -r["peak_context_tokens"])
    return rows[:max_sessions], skipped_active


def load_referral():
    """Optional enrichment. Absent, stale or unknown-schema means run standalone
    with no complaint — cost-inspector is never a prerequisite."""
    try:
        with open(INSPECTOR_FINDINGS, encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    if data.get("schema_version") not in KNOWN_SCHEMA_VERSIONS:
        return {"status": "unrecognised_schema"}
    written = parse_ts(data.get("written_at"))
    if written:
        age = (dt.datetime.now(dt.timezone.utc) - written).days
        if age > REFERRAL_MAX_AGE_DAYS:
            return {"status": "stale", "age_days": age,
                    "written_at": data.get("written_at")}
    ref = data.get("referral") or {}
    return {
        "status": "ok",
        "written_at": data.get("written_at"),
        "recommended": ref.get("recommended"),
        "reason": ref.get("reason"),
        "signals": ref.get("signals") or [],
    }


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--days", type=int, default=DEFAULT_SELECT_DAYS,
                    help="stage-1 selection window (default 30)")
    ap.add_argument("--max-sessions", type=int, default=DEFAULT_SELECT_SESSIONS,
                    help="stage-1 candidate cap (default 100)")
    ap.add_argument("--read", type=int, default=DEFAULT_READ_COUNT,
                    help="stage-2 sessions to actually read (default 5)")
    ap.add_argument("--include-active", action="store_true",
                    help="also consider sessions touched in the last "
                         f"{ACTIVE_WINDOW_MINUTES} min (normally skipped - one "
                         "of them is the session running this review)")
    ap.add_argument("--manifest", help="write the authoritative stage-2 manifest here")
    args = ap.parse_args()

    rows, skipped_active = scan_sessions(args.days, args.max_sessions,
                                         args.include_active)
    chosen = rows[:max(0, args.read)]
    forecast = round(sum(r["read_cost_usd"] for r in chosen), 2)

    out = {
        "stage1": {
            "window_days": args.days,
            "max_sessions": args.max_sessions,
            "candidates_found": len(rows),
            "cost_usd": 0.0,
        },
        "stage2": {
            "read_count": len(chosen),
            "review_model": REVIEW_MODEL,
            "forecast_usd": forecast,
            "sessions": [
                {k: r[k] for k in ("session_id", "project", "last_active",
                                   "turns", "peak_context_tokens",
                                   "read_cost_usd")}
                for r in chosen
            ],
            "paths": [r["path"] for r in chosen],
        },
        "referral": load_referral(),
        "skipped_active_sessions": skipped_active,
    }
    if args.manifest:
        # Authoritative record of what stage 2 will read. report.py uses this
        # for the "what was read" table so the reviewing model cannot
        # misreport turn counts or coverage.
        with open(args.manifest, "w", encoding="utf-8") as fh:
            json.dump({"sessions": out["stage2"]["sessions"],
                       "review_model": REVIEW_MODEL,
                       "forecast_usd": forecast,
                       "skipped_active_sessions": skipped_active}, fh, indent=2)
    print(json.dumps(out, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
