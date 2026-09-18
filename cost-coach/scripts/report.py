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
    "vague-opening": "An unclear first message means Claude asks again or "
                     "builds the wrong thing - you pay for both.",
    "rework-loop": "Work that gets built, undone, then rebuilt is paid for "
                   "every time.",
    "model-mismatch": "The model is fixed when a session starts, so the "
                      "expensive one also handles the easy parts.",
    "missed-delegation": "Search results left in the main conversation get paid "
                         "for again on every later message; a helper subagent "
                         "returns only the answer.",
    "context-churn": "Everything said earlier is re-sent with every new "
                     "message, so old material you no longer need still costs "
                     "you.",
    "tool-thrash": "Many small repeated actions cost far more than one batched "
                   "action.",
    "abandoned-work": "Sessions that end with nothing kept are the most "
                      "expensive kind.",
}


def _s(n):
    """Plural suffix. '5 habit(s)' reads like a form, not a sentence."""
    return "" if n == 1 else "s"


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
    out = ["# How you worked", ""]
    out.append("*Habits from your own conversations that cost money. Each one "
               "quotes the moment it came from, so you can check it.*")
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
        note = f"> A sample of {len(reviewed)}, not everything - habits repeat."
        if partial:
            note += (f" **{len(partial)} {'was' if len(partial) == 1 else 'were'} "
                     "too long to read in full**, so findings from "
                     f"{'it' if len(partial) == 1 else 'those'} cover the start "
                     "only.")
        if authoritative and total_cost:
            note += f" Reading them cost about ${total_cost:.2f}."
        skipped = (manifest or {}).get("skipped_active_sessions") or []
        if skipped:
            note += (f" {len(skipped)} still-running "
                     f"conversation{_s(len(skipped))} skipped.")
        out.append(note)
        out.append("")

    if not findings:
        out.append("## Nothing worth changing")
        out.append("")
        out.append("The conversations we read look efficient. We would rather "
                   "say that than invent advice.")
        out.append("")
    else:
        out.append(f"## {len(findings)} habit{_s(len(findings))} worth "
                   "knowing about")
        out.append("")
        for f in findings:
            lens = str(f.get("lens", ""))
            title = LENS_TITLES.get(lens, lens.replace("-", " ").capitalize())
            sev = str(f.get("severity", "")).lower()
            out.append(f"### {title}")
            out.append("")
            meta = f"`{SEVERITY_LABEL.get(sev, 'worth a look')}`"
            meta += f" \u00b7 `{str(f.get('session_id'))[:8]}`"
            if f.get("timestamp"):
                meta += f" \u00b7 {f['timestamp']}"
            out.append(meta)
            out.append("")
            for line in str(f["excerpt"]).strip().splitlines():
                out.append(f"> {line}")
            out.append("")
            if LENS_BLURB.get(lens):
                out.append(LENS_BLURB[lens])
                out.append("")
            out.append(f"**Cost:** {f['what_it_cost']}")
            out.append("")
            out.append(f"**Next time:** {f['do_differently']}")
            out.append("")

    if data.get("notes"):
        out.append("## Notes")
        out.append("")
        out.append(str(data["notes"]))
        out.append("")

    if rejected:
        out.append("## Dropped")
        out.append("")
        out.append("Points we could not tie to a specific moment, so we left "
                   "them out rather than ask you to take them on trust:")
        out.append("")
        for idx, reason in rejected:
            out.append(f"- point #{idx + 1}: {reason}")
        out.append("")

    out.append("---")
    out.append("")
    out.append("*Read from your own conversations with obvious secrets stripped "
               "first - best-effort, not a guarantee, so keep this file private. "
               "These are judgement calls, not arithmetic, which is why each one "
               "quotes its source.*")
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
