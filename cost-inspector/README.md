# cost-inspector

**Finds out where your Claude Code money is going, and fixes the safe parts.**

Claude Code can tell you what you spent — `/usage` gives you the number. What it
does not tell you is *why*: which settings and which habits produced that
number, and which of them are worth changing.

This skill looks at how your setup is configured and how you have been using it,
then hands you a list of what is wasting money — with a dollar figure on each
item and a plain explanation of why it costs anything.

It runs 16 checks. Things like:

- Skills and plugins that load on every single message but you never use
- Instruction files (`CLAUDE.md`) long enough to cost you all day
- Connected services you have not called in a month
- Your model and thinking-effort settings
- How much of your text is being paid for twice instead of re-used from cache
- Habits: lots of screenshots, the same file read repeatedly, declined commands

Every finding says **who fixes it**: some the skill can do for you, some need a
judgement call from you, some are just a habit to change.

## What it does not do

**It never reads your conversations.** Only settings files and counters — token
totals, model names, tool names, timestamps. If you want feedback on how you
actually work, that is a separate skill you install on purpose:
[cost-coach](../cost-coach).

## Install

```bash
git clone https://github.com/liorarieli-maker/Skills.git /tmp/liorar-skills
cp -r /tmp/liorar-skills/cost-inspector ~/.claude/skills/
```

Then in Claude Code:

```
/cost-inspector
```

Python 3, no packages to install. It reads only; nothing is changed unless you
run an apply command yourself, and a dated backup is saved first.

## About the dollar figures

They are worked out from public list prices, which usually come out **higher
than a real bill** — about 1.8x higher on the one account this was tested on.
Discounts and plans are not visible on your machine, so they cannot be guessed.

Tell it what you were actually billed and every figure gets corrected:

```bash
python3 ~/.claude/skills/cost-inspector/scripts/audit.py \
  --actual-spend 120.00 --spend-since 2026-09-01 --spend-asof 2026-09-18T09:30
```

It remembers that afterwards. Either way the *order* of the findings is
trustworthy — being off by the same factor everywhere does not change which
problem is biggest.

## Optional: the first-message reminder

Claude Code picks your model and effort once per session and never changes them
by itself, so an expensive default also handles the trivial work. This installs
a hook that runs once, on your first message of a session, and speaks up only
when the task clearly does not need what you are paying for:

```bash
python3 ~/.claude/skills/cost-inspector/scripts/audit.py --install-hook
# undo with --remove-hook
```

It stays silent otherwise — including when it is unsure. That is model
behaviour, though, not a guarantee.

## Honest limitations

- The session-history format is internal to Claude Code and changes between
  releases. When something cannot be read, it reports "could not tell" rather
  than guessing a zero.
- Records of helper subagents live in a temporary folder your computer clears
  out, so older helper costs are only partly visible.
- The cost of an unused connected service is not published by Anthropic, so
  that one figure is an estimate built from the services you do use — and it is
  a floor, not a measurement.
- Savings from switching model or effort are **upper bounds**. They assume the
  cheaper option would have finished the same work.
- Running this skill is not free either — it costs a few cents per run, mostly
  reading its own instructions and the report back to you.
