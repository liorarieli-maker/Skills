---
name: cost-inspector
description: Check how this machine's Claude Code is set up, find where money is being wasted, and fix the safe ones. Gives a plain-language checklist with a dollar figure on each item - skills and plugins that load on every message but are never used, instruction files that are too long, connected services nobody calls, the model and thinking-effort settings, how much text is being paid for twice, and costly habits. Use when the user asks why Claude Code is expensive, wants to cut their token use or bill, asks what is filling up the conversation, or asks for their setup to be reviewed. Reads settings and usage counters only - never the text of conversations.
disable-model-invocation: true
---

# cost-inspector

Audits a Claude Code setup for cost waste and explains each finding so the user
does not need the tool twice for the same problem.

**Privacy boundary — state it if asked, and never cross it.** This skill reads
configuration files and transcript *metadata*: token counts, model names, tool
names, result sizes, timestamps, file paths. It never reads the text of
conversations. Conversation-level feedback is a separate skill (`cost-coach`)
that the user installs deliberately.

## Step 0 — ask for their real spend BEFORE running

Do not run the audit cold. First check whether a calibration is already saved
(cheap, no transcript scan):

```bash
python3 scripts/audit.py --calibration-status
```

- **`"action": "proceed"`** — a calibration is saved and will be applied
  automatically. Run the audit normally.
- **`"action": "ask_user"`** — nothing saved. **Ask the user for their actual
  spend first**, then run with it. Say plainly why: without it every dollar
  figure is a list price, measured at ~1.8x a real bill on one Enterprise
  account. Tell them where to look — their Claude usage/limits panel shows
  "$X of $Y spent" and a reset date, which gives you both the amount and the
  billing-period start.
- **`"action": "ask_user_refresh"`** — the saved figure is over 14 days old.
  Ask for a fresh one; offer to proceed with the stale figure if they would
  rather not look it up.

If the user declines or does not know, run anyway — but lead with the ranking
rather than the dollar amounts, since the ranking is unaffected by a uniform
rate error. Never present an uncalibrated total as if it were their bill.

## Running it

```bash
python3 scripts/audit.py            # default: last 30 days
python3 scripts/audit.py --days 7
python3 scripts/audit.py --json     # machine-readable

# Calibrate against a real bill (strongly recommended - see below)
python3 scripts/audit.py --actual-spend 120.00 --spend-since 2026-09-01 \
  --spend-asof 2026-09-18T09:30
```

Stdlib only, no dependencies, read-only unless `--apply` is passed.

Two files are written, for two different readers — do not confuse them:

| File | Reader | Format |
|---|---|---|
| `~/.claude/cost-inspector/last-report.md` | the user | markdown |
| `~/.claude/cost-inspector/last-audit.json` | `cost-coach` | JSON, `schema_version` 1 |

The report is printed to the terminal as fixed-width text and saved as real
markdown (`--report PATH` to relocate, `--no-report` to skip). The JSON is the
inter-skill handoff and is not meant to be read by a person.

## Dollar figures are indicative — say so

List prices are **not** what people are billed. Measured against one Enterprise
account, the list-price model came out **1.82× the actual billed amount**;
negotiated rates and plan discounts all push the same direction.

So:

- **Never present the headline number as a bill.** Uncalibrated, it is an upper
  bound. The report labels it "Estimated spend" and prints the caveat.
- **Ask the user for their real spend and re-run with `--actual-spend`.** On a
  usage-billed plan this is visible in their usage/limits panel. The report then
  says "Calibrated" and scales every finding by the same factor.
- **Always pass `--spend-asof` unless the figure was read just now.** The
  calibration period must be bounded at *both* ends. Without an upper bound,
  usage accrued after the user read their bill inflates the denominator and the
  factor decays silently on every later run — observed drifting from `x0.544`
  to `x0.496` in a few hours of work, biasing every finding low. `--spend-since`
  sets the start (billing period), `--spend-asof` the moment of the reading.
- **The ranking is trustworthy either way.** A uniform rate error scales all
  figures equally, so which finding matters most, and by what ratio, is correct
  even uncalibrated. Lead with that when you have no real number to calibrate
  against.
- On a flat-fee subscription, dollars represent the value of capacity consumed
  rather than a charge.

Print the report as the script produces it. Do not rewrite the numbers or
re-summarise the findings in prose — the formatting is the deliverable, and
paraphrasing invites drift from the measured values.

## Presenting the results

1. **Lead with the header block.** Spend, always-on extra, model mix, re-used
   text.
2. **Failures expand, passes collapse.** The script already does this.
3. **Never inflate.** Every dollar figure comes from the script's arithmetic.
   Do not add estimates of your own, and do not extrapolate to annual figures
   unless asked.
4. **"Nothing to fix" is a real result.** If the report is mostly passes, say
   so plainly. Do not hunt for something to recommend.

## Write for someone who is not a power user

Assume the reader uses Claude Code most days and has never read its docs. They
do not know what a token, a turn, context, the cache, compaction, a subagent, an
MCP server or frontmatter is. The report's own wording already accounts for
this — **match it in the chat instead of reverting to shorthand.**

- Do not use the check IDs (`A4`, `B1`, `B3`) when talking to the user. They are
  handles for `--apply`, not names. Use the finding's title.
- First mention of anything Claude Code specific gets three words of
  explanation: "a subagent (a helper Claude that does one job and reports
  back)".
- Say "message" not "turn", "instructions file" not "CLAUDE.md" on first use,
  "text re-used from the cache" not "cache hit rate".
- When they ask what a finding means, explain the mechanism in two sentences and
  stop. The report already carries the long version and a docs link.
- Tell them what to do in words they could act on without you. A command they
  can paste, or a sentence they can say to Claude, beats a principle.

## Two classes of number — keep them distinct

- **Category A (overhead)** is arithmetic: tokens per turn × turns × rate, priced
  at the *cache-read* rate because always-on overhead lives in the cached
  prefix. No behavioural assumption.
- **Category B (model / effort)** is a counterfactual. Repricing Opus work at
  Sonnet rates assumes Sonnet would have finished the same work. It is an
  **upper bound** — say so whenever quoting it.

Getting this wrong is the single biggest credibility risk: pricing overhead at
the fresh-input rate overstates savings by roughly 7–10×.

## Applying fixes

Only findings marked `AUTO` can be applied by the script:

```bash
python3 scripts/audit.py --apply A4,A5
```

- **`AUTO`** — mechanical and reversible. `A4` (skill frontmatter), `A5`
  (`enabledPlugins`), `C2` (compaction settings).
- **`A7` (auto-memory)** is `ASSISTED`: trimming a `MEMORY.md` is a judgement
  call about which memories still matter. Note that `MEMORY.md` is always-on
  overhead while the individual memory files load on demand — never price
  those as per-turn cost.
- **`ASSISTED`** — propose the edit, let the user decide. Splitting a CLAUDE.md
  is a judgement call about which rules belong where.
- **`MANUAL`** — print the command; the script cannot run it.
- **`NONE`** — diagnostic only.

Rules when applying:

- **Ask first, per finding.** Never apply everything because the user said yes
  to one thing.
- A timestamped `.bak-<stamp>` copy is written before any edit.
- **Never write inside `~/.claude/plugins/`.** Plugin updates overwrite it, so
  the fix would vanish silently. Plugins are disabled via the `enabledPlugins`
  map in `settings.json` instead.
- If the user is on managed/enterprise settings, a write can appear to succeed
  while being overridden. Mention this if a fix seems not to take effect.

## Honest limitations — volunteer these, don't wait to be caught

- **Transcript format is internal** and changes between releases. The script
  version-guards and degrades to `UNKNOWN` rather than reporting a wrong zero.
- **Subagent transcripts are ephemeral** (they live in a temp directory), so
  historical subagent spend is only partly visible and totals undercount.
- **MCP per-turn cost is unpublished.** `A6` therefore prints an *estimate*
  extrapolated from the user's own used servers, and states that basis in the
  finding. Quote it as an estimate and a floor, never as a measurement.
- **No per-file token readout exists** — before/after is the script's own
  arithmetic, not a reading from Claude Code.
- Dollar figures are **API-equivalent**. On a subscription they represent the
  value of capacity consumed rather than a bill.

## Referral to cost-coach

The script ends with one line about whether a conversation review looks
worthwhile, based on the category-D signals, and includes what it would cost.
When it says the patterns look efficient, pass that on as-is — recommending the
other skill anyway turns a referral into an advert.

Findings are written to `~/.claude/cost-inspector/last-audit.json`
(`schema_version` 1) which `cost-coach` reads if present. Writing it is
unconditional; nothing here depends on that skill being installed.
