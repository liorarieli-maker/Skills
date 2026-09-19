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
  account.

  Ask for **three** things, not one — the output carries them in
  `ask_user_for`:

  1. the amount billed so far this period
  2. the date the period started (`--spend-since`)
  3. the time they read it (`--spend-asof`) — without this the factor decays
     on every later run

  **Name the exact screen for their plan.** "Your usage panel" is not an
  answer; the place differs, and the output gives all three in
  `how_to_find_it`:

  | Plan | Where |
  |---|---|
  | Pro / Max | `/usage-credits`, or claude.ai → Settings → Usage → Usage credits |
  | Team / Enterprise | claude.ai → Admin settings → Usage, or the org spend report from their admin |
  | API / Console | platform.claude.com/usage |

- **`"action": "proceed_managed_rates"`** — an admin has published the
  organisation's contracted rates in the `modelPricing` managed setting, so
  Claude Code is already using real rates. Do not ask for a spend figure;
  say the figures are already close and run the audit.
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

The script writes fixed-width text to stdout and saves the same report as real
markdown (`--report PATH` to relocate, `--no-report` to skip). The JSON is the
inter-skill handoff and is not meant to be read by a person.

**The stdout copy is for you, not for them** — it lands inside a collapsed tool
block. The markdown file is the one the user can actually open, so its path
belongs in your reply. See *The user cannot see your tool output* below.

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

## The user cannot see your tool output

**Running the script shows the user nothing.** Bash output is collapsed behind a
"Ran N commands" toggle, so from where they are sitting the report does not
exist until you put it in your reply.

**Never write "the full report is printed above", "see the output above", or
anything similar.** It is not above. That sentence is the single most common way
this skill fails: the user is told to read something they cannot see.

Every run ends with, in this order:

1. **A one-line TL;DR** — total avoidable spend, and the one biggest cause.
2. **The savings table, pasted into your reply.** Copy the `What we found /
   Saves per month / Who does it` table out of the generated markdown. Copy it;
   do not retype the numbers.
3. **The saved report, offered as something they can actually read.** A path on
   its own is a dead end — you are telling them where the report is instead of
   giving it to them, and they have said so. Give the path
   (`~/.claude/cost-inspector/last-report.md`), say it holds the full reasoning
   and fix command for every finding, and **offer to open it in the same
   breath**:

   > Full report: `~/.claude/cost-inspector/last-report.md` — say the word and
   > I'll open it, or paste any section here.

   If they accept, re-run with `--open`, which opens it in their default
   markdown viewer. Costs no tokens and nothing leaves the machine. Do not pass
   `--open` uninvited: opening a window unasked is rude, and on a headless box
   it does nothing.
4. **The offer to apply what is automatic**, one finding at a time.

## Presenting the results

1. **Lead with the bottom line**, then the table. Not the methodology.
2. **Failures expand, passes collapse.** The script already does this; one line
   saying how many checks passed is enough.
3. **Never inflate.** Every dollar figure comes from the script's arithmetic.
   Do not add estimates of your own, and do not extrapolate to annual figures
   unless asked.
4. **"Nothing to fix" is a real result.** If the report is mostly passes, say
   so plainly. Do not hunt for something to recommend.
5. **Do not re-summarise the findings in your own prose** beyond the TL;DR. The
   generated wording is the deliverable and paraphrasing drifts from the
   measured values.

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
  (`enabledPlugins`). **`C2` is not auto-appliable** and no longer touches any
  setting: it is a habit, and the fix is `/clear` at the right moment.
- **`A7` (auto-memory)** is `ASSISTED`: trimming a `MEMORY.md` is a judgement
  call about which memories still matter. Note that `MEMORY.md` is always-on
  overhead while the individual memory files load on demand — never price
  those as per-turn cost.
- **`ASSISTED`** — propose the edit, let the user decide. Splitting a CLAUDE.md
  is a judgement call about which rules belong where. Prefer `skillOverrides`
  for a single unused skill inside a plugin over disabling the whole plugin.
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

**`C2` is the clearest case of what this skill cannot do.** It can measure that
a session carried a large conversation for hundreds of messages, and price it.
It cannot tell whether that history was still being used. Its dollar figure
therefore assumes the worst — that everything above the line was dead weight —
which makes it an upper bound, not an estimate. Say so when presenting it.
`cost-coach` reads the conversation and can find the actual moment the old
material stopped earning its keep, so a `C2` finding is a good reason to
mention the referral. Never present the `C2` figure as what the user would
have saved.

Findings are written to `~/.claude/cost-inspector/last-audit.json`
(`schema_version` 1) which `cost-coach` reads if present. Writing it is
unconditional; nothing here depends on that skill being installed.
