#!/usr/bin/env python3
"""cost-coach stage 3 — render the subagent's findings as a markdown report.

Takes structured findings on stdin or from a file and writes markdown. The
schema is the enforcement mechanism for this skill's central rule: a finding
with no quoted excerpt cannot be rendered, so unfalsifiable advice cannot ship.
"""

import argparse
import datetime as dt
import json
import os
import sys

HOME = os.path.expanduser("~")
REPORT_PATH = os.path.join(HOME, ".claude", "cost-coach", "last-review.md")

REQUIRED = ("lens", "session_id", "excerpt", "what_it_cost", "do_differently")
SEVERITY_ORDER = {"high": 0, "medium": 1, "low": 2}
# "high / medium / low" reads like a bug tracker. Say what it means for them.
SEVERITY_LABEL = {
    "high": "costs you real money",
    "medium": "worth changing",
    "low": "minor",
}

# The lens keys are internal identifiers. What the reader sees has to say what
# happened without knowing any Claude Code vocabulary, so each key gets a plain
# title and a one-line explanation of the pattern itself.
LENS_TITLES = {
    "vague-opening": "The request was not clear enough at the start",
    "rework-loop": "The same work was done more than once",
    "model-mismatch": "An expensive model did simple work",
    "missed-delegation": "A big search ran in the main conversation",
    "context-churn": "A new subject started in the same conversation",
    "tool-thrash": "The same kind of action repeated many times",
    "abandoned-work": "A session cost a lot and produced nothing",
}

LENS_BLURB = {
    "vague-opening": "When the first message leaves room for guessing, Claude "
                     "asks follow-up questions, or guesses wrong and builds the "
                     "wrong thing. You pay for both.",
    "rework-loop": "Work that gets built, then undone, then rebuilt is paid for "
                   "every time. Usually it means the goal was not pinned down "
                   "before the work started.",
    "model-mismatch": "The model is chosen once per session and does not change "
                      "by itself. So the most expensive model also handles the "
                      "easy, mechanical parts of the session.",
    "missed-delegation": "Searching in the main conversation leaves every raw "
                         "result sitting there, and you pay for all of it again "
                         "on every later message. A helper subagent does the "
                         "same search separately and returns only the answer.",
    "context-churn": "Everything said earlier in a conversation is re-sent with "
                     "every new message. When the subject changes, all the old "
                     "material is still being paid for and no longer helps.",
    "tool-thrash": "Many small repeated actions cost far more than one batched "
                   "action, and each result stays in the conversation.",
    "abandoned-work": "Sessions that end with nothing kept are the most "
                      "expensive kind. Usually the direction was wrong early "
                      "and nobody stopped to check.",
}


def validate(findings):
    """Split into renderable and rejected. Missing evidence is a rejection,
    not a warning - that is the whole point of validating here."""
    good, rejected = [], []
    for i, f in enumerate(findings):
        if not isinstance(f, dict):
            rejected.append((i, "not an object"))
            continue
        missing = [k for k in REQUIRED if not str(f.get(k) or "").strip()]
        if missing:
            rejected.append((i, "missing " + ", ".join(missing)))
            continue
        good.append(f)
    good.sort(key=lambda f: SEVERITY_ORDER.get(str(f.get("severity", "")).lower(), 3))
    return good, rejected


def render(data, findings, rejected, manifest=None):
    # The manifest (from select.py + extract.py) is AUTHORITATIVE for coverage.
    # The reviewing model only sees a possibly-truncated extract, so its own
    # turn counts understate the session and would imply a fuller read than
    # actually happened.
    reviewed = (manifest or {}).get("sessions") or data.get("reviewed") or []
    authoritative = bool((manifest or {}).get("sessions"))
    out = ["# How you worked — session review", ""]
    out.append("*This report read a few of your past Claude Code conversations "
               "and looked for habits that cost money. Every point below quotes "
               "the actual moment it came from, so you can check it yourself and "
               "disagree if we got it wrong.*")
    out.append("")
    out.append(f"Run on {dt.datetime.now().strftime('%d %b %Y at %H:%M')}.")
    out.append("")

    if reviewed:
        out.append("## Which conversations we read")
        out.append("")
        if authoritative:
            out.append("| Session | Project | Messages | How much we read "
                       "| Cost to read |")
            out.append("|---|---|---|---|---|")
            for r in reviewed:
                cov = "part of it" if r.get("truncated") else "all of it"
                cost = r.get("read_cost_usd")
                out.append(
                    f"| `{str(r.get('session_id',''))[:8]}` "
                    f"| {r.get('project','?')} "
                    f"| {r.get('turns','?')} "
                    f"| {cov} "
                    f"| {'$%.2f' % cost if isinstance(cost,(int,float)) else '?'} |")
        else:
            out.append("| Session | Project | Messages |")
            out.append("|---|---|---|")
            for r in reviewed:
                out.append(f"| `{str(r.get('session_id',''))[:8]}` | "
                           f"{r.get('project','?')} | {r.get('turns','?')} |")
        out.append("")
        partial = [r for r in reviewed if r.get("truncated")]
        total_cost = sum(r.get("read_cost_usd") or 0 for r in reviewed)
        note = (f"> We read {len(reviewed)} of your conversations, not all of "
                "them. Habits tend to repeat, so a few is usually enough to see "
                "the pattern - but treat this as a sample, not the full story.")
        if partial:
            note += (f" **{len(partial)} of them were too long to read in full**, "
                     "so anything we say about those covers the beginning of the "
                     "conversation only.")
        if authoritative and total_cost:
            note += (f" Reading them cost about ${total_cost:.2f} - reading "
                     "conversations is not free, which is why we only sample.")
        out.append(note)
        out.append("")
        skipped = (manifest or {}).get("skipped_active_sessions") or []
        if skipped:
            out.append(f"> We left out {len(skipped)} conversation(s) that were "
                       "still going, including this one.")
            out.append("")

    if not findings:
        out.append("## The short version")
        out.append("")
        out.append("**Nothing worth changing.** The conversations we read look "
                   "efficient. We would rather tell you that than invent advice.")
        out.append("")
    else:
        out.append("## The short version")
        out.append("")
        out.append(f"We found {len(findings)} habit(s) worth knowing about. Each "
                   "one is explained below, with the moment it came from.")
        out.append("")
        out.append("| How much it matters | What happened | Where |")
        out.append("|---|---|---|")
        for f in findings:
            lens = str(f.get("lens", ""))
            title = LENS_TITLES.get(lens, lens.replace("-", " ").capitalize())
            sev = str(f.get("severity", "")).lower()
            out.append(f"| {SEVERITY_LABEL.get(sev, 'worth a look')} | {title} "
                       f"| `{str(f.get('session_id'))[:8]}` |")
        out.append("")
        out.append("## Each one, explained")
        out.append("")
        for f in findings:
            lens = str(f.get("lens", ""))
            title = LENS_TITLES.get(lens, lens.replace("-", " ").capitalize())
            sev = str(f.get("severity", "")).lower()
            badge = f" — {SEVERITY_LABEL[sev]}" if sev in SEVERITY_LABEL else ""
            out.append(f"### {title}{badge}")
            out.append("")
            if LENS_BLURB.get(lens):
                out.append(f"*Why this pattern costs money:* {LENS_BLURB[lens]}")
                out.append("")
            where = f"`{str(f.get('session_id'))[:8]}`"
            if f.get("timestamp"):
                where += f", {f['timestamp']}"
            out.append(f"**Where it happened:** {where}")
            out.append("")
            out.append("**What we saw in your conversation:**")
            out.append("")
            for line in str(f["excerpt"]).strip().splitlines():
                out.append(f"> {line}")
            out.append("")
            out.append(f"**What it cost you:** {f['what_it_cost']}")
            out.append("")
            out.append(f"**What to do next time:** {f['do_differently']}")
            out.append("")

    if data.get("notes"):
        out.append("## Notes")
        out.append("")
        out.append(str(data["notes"]))
        out.append("")

    if rejected:
        out.append("## Things we chose not to tell you")
        out.append("")
        out.append("We dropped the points below because we could not point at "
                   "the moment they came from. Advice you cannot check is not "
                   "worth much, so we would rather leave it out:")
        out.append("")
        for idx, reason in rejected:
            out.append(f"- point #{idx + 1}: {reason}")
        out.append("")

    out.append("---")
    out.append("")
    out.append("*How this was made: we read a sample of your own conversations "
               "with the obvious secrets stripped out first - passwords, keys, "
               "card numbers and the like. That stripping is best-effort, not a "
               "guarantee, so treat this file as private. Everything above is a "
               "judgement call, which means it can be wrong in a way arithmetic "
               "cannot; that is exactly why each point quotes the moment it came "
               "from, so you can decide for yourself.*")
    return "\n".join(out)


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--findings", help="JSON file (default: stdin)")
    ap.add_argument("--out", help=f"output path (default {REPORT_PATH})")
    ap.add_argument("--manifest", help="authoritative stage-1/2 manifest "
                                       "(overrides the model's coverage claims)")
    ap.add_argument("--print", action="store_true", help="also print to stdout")
    args = ap.parse_args()

    raw = open(args.findings, encoding="utf-8").read() if args.findings \
        else sys.stdin.read()
    try:
        data = json.loads(raw)
    except ValueError as exc:
        print(f"findings are not valid JSON: {exc}", file=sys.stderr)
        return 2
    if not isinstance(data, dict):
        print("expected a JSON object with a 'findings' array", file=sys.stderr)
        return 2

    manifest = None
    if args.manifest:
        try:
            with open(args.manifest, encoding="utf-8") as fh:
                manifest = json.load(fh)
        except (OSError, ValueError):
            manifest = None

    findings, rejected = validate(data.get("findings") or [])
    md = render(data, findings, rejected, manifest)

    dest = args.out or REPORT_PATH
    os.makedirs(os.path.dirname(os.path.abspath(dest)), exist_ok=True)
    with open(dest, "w", encoding="utf-8") as fh:
        fh.write(md + "\n")
    try:
        os.chmod(dest, 0o600)  # contains excerpts of the user's own work
    except OSError:
        pass

    if args.print:
        print(md)
    print(f"report saved to {dest.replace(HOME, '~')}"
          f" ({len(findings)} findings"
          + (f", {len(rejected)} dropped for no evidence" if rejected else "")
          + ")", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
