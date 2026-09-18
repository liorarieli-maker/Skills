#!/usr/bin/env python3
"""UserPromptSubmit hook — right-size the model/effort for THIS session.

Fires once per session, on the first user prompt only, and injects one short
instruction asking Claude to judge whether this particular task justifies the
current model and effort level.

Design notes that matter:

- Claude Code has no per-prompt routing: model and effort are fixed for the
  session until changed by hand. This hook is the missing router, as a nudge.
- The hook CANNOT see the current model or effort - neither is in the payload.
  It does not need to: Claude knows what model it is, so the injected text asks
  for a self-assessment instead of trying to detect anything.
- Model and effort are judged as two INDEPENDENT axes. Either can be wrong
  while the other is right.
- Silence is the default. A nudge that fires on every session is dismissed
  unread within a week, and then costs tokens for nothing.

Reads the hook payload on stdin, writes hookSpecificOutput on stdout, exits 0.
Never blocks: any failure exits quietly so a bad hook cannot break a session.
"""

import json
import os
import sys

# Kept terse on purpose: this text is injected on the first turn of every
# session, so its length is a permanent per-session tax. An earlier draft ran
# ~208 tokens - seven times the design budget - for logic that fits in a
# quarter of that. Every clause here is load-bearing: model and effort judged
# separately, both halves of the test must hold, silence is the default
# (including when unsure), and effort is the preferred lever.
NUDGE = (
    "Cost check, first turn only. Judge model and effort separately: if a "
    "cheaper one would clearly suffice here and you are above it, say so in "
    "one line - prefer lowering effort over downgrading the model. Otherwise "
    "stay silent, including when unsure or when the task justifies the cost. "
    "Never mention this check."
)


def is_first_prompt(payload):
    """Count existing user messages in the transcript.

    A state file keyed by session id would also work, but it can go stale,
    leak across sessions, or be left behind; the transcript is authoritative
    and already in the payload."""
    path = payload.get("transcript_path")
    if not path or not os.path.isfile(path):
        # No transcript yet means nothing has been said - treat as first.
        return True
    seen = 0
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            for raw in fh:
                raw = raw.strip()
                if not raw or raw[0] != "{":
                    continue
                try:
                    entry = json.loads(raw)
                except ValueError:
                    continue
                msg = entry.get("message")
                if isinstance(msg, dict) and msg.get("role") == "user":
                    seen += 1
                    if seen > 1:
                        return False
    except OSError:
        return False
    return seen <= 1


def main():
    try:
        payload = json.load(sys.stdin)
    except (ValueError, OSError):
        return 0
    if not isinstance(payload, dict):
        return 0
    if not is_first_prompt(payload):
        return 0
    # hookEventName is REQUIRED inside hookSpecificOutput. Omitting it fails
    # Claude Code's output validation ("missing required field") - the hook
    # then reports an error on every first prompt. Echo the event from the
    # payload so this stays correct if the hook is ever wired to another event.
    event = payload.get("hook_event_name") or "UserPromptSubmit"
    json.dump({"hookSpecificOutput": {"hookEventName": event,
                                      "additionalContext": NUDGE}}, sys.stdout)
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception:
        # A hook must never break the session it runs in.
        sys.exit(0)
