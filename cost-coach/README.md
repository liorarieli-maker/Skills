# cost-coach

**Reads a few of your past Claude Code conversations and tells you which of
your habits cost money.**

Its companion, [cost-inspector](../cost-inspector), looks at your settings and
your totals. It cannot see *how* you work. This one can, because it actually
reads your conversations.

It looks for patterns like:

- The first message was not clear enough, so work started in the wrong direction
- The same work got built, undone, then built again
- An expensive model spent a session doing simple, mechanical edits
- A big search ran in the main conversation, so every raw result stayed there
  and got paid for again on every later message
- A new subject started in the same conversation, dragging all the old material
  along
- The same small action repeated dozens of times instead of being grouped
- A session that cost a lot and produced nothing anyone kept

**Every point quotes the actual moment it came from**, so you can check it and
disagree. Feedback you cannot verify is worth nothing, so anything the skill
cannot point at gets dropped from the report rather than shipped.

If it finds nothing, it says so. That is a real result, not a failure.

## Read this before you install it

**This skill reads the text of your conversations.** That is the whole point of
it, and also the thing to think about first:

- It always asks permission and shows you the cost **before** reading anything.
- It tells you exactly which conversations it wants to read, by project and date.
- Obvious secrets — passwords, keys, tokens, card numbers — are stripped out
  before anything is sent. That stripping is **best-effort, not a guarantee.**
- Your conversations may contain other people's information: an employer's
  code, a customer's data. You can agree on your own behalf, not your
  company's. Think about which conversations these are.
- The saved report quotes your own work, so treat the file as private.

**It is not free.** Reading conversations costs real money — usually a few
dollars for a handful of sessions, and it tells you the figure before it starts.
This is exactly why it is a separate skill from `cost-inspector`, which is
cheap enough to run whenever you like. You run this one occasionally, on
purpose.

## Install

```bash
git clone https://github.com/liorarieli-maker/Skills.git /tmp/liorar-skills
cp -r /tmp/liorar-skills/cost-coach ~/.claude/skills/
```

Then in Claude Code:

```
/cost-coach
```

Python 3, no packages to install. Your conversation files are only ever read,
never changed.

## How it works

Three stages, so nothing expensive happens by accident:

1. **Picking sessions — free.** Ranks your recent sessions by cost using
   counters only. Nothing has been read yet. Conversations still in progress
   are skipped, including the one you are running this from.
2. **Reading — costs money.** Only after you say yes, and only the sessions you
   agreed to. The reading is done by a cheap model in a separate helper, so the
   raw text never lands in your own conversation, where it would inflate the
   very cost it is commenting on.
3. **The report — free.** Saved to `~/.claude/cost-coach/last-review.md`.

It works on its own, but if you have `cost-inspector` installed it will pick up
that skill's findings and answer them directly — including telling you when the
inspector was wrong.

## Honest limitations

- **It only reads a few conversations.** Habits repeat, so a sample usually
  shows the pattern, but it is not the full picture — and the report says how
  many it read.
- **Very long conversations are only read in part**, and the report says which
  ones.
- **This is judgement, and judgement can be wrong** in a way arithmetic cannot.
  That is why every point quotes its source.
- Much of what it finds is Claude's behaviour rather than yours. The report says
  so when that is the case, instead of reading like an accusation.
