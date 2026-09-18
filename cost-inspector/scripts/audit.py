#!/usr/bin/env python3
"""cost-inspector — audit a Claude Code setup for token/cost waste.

Reads configuration and transcript METADATA only. Never reads conversation text.
Stdlib only. Read-only unless --apply is passed.
"""

import argparse
import collections
import datetime as dt
import glob
import json
import os
import re
import shutil
import subprocess
import sys

SCHEMA_VERSION = 1
HOME = os.path.expanduser("~")
CLAUDE_DIR = os.path.join(HOME, ".claude")
FINDINGS_PATH = os.path.join(CLAUDE_DIR, "cost-inspector", "last-audit.json")
REPORT_PATH = os.path.join(CLAUDE_DIR, "cost-inspector", "last-report.md")
CONFIG_PATH = os.path.join(CLAUDE_DIR, "cost-inspector", "config.json")
CALIBRATION_STALE_DAYS = 14

# Claude Code versions this parser has been exercised against. Outside this
# range the transcript layout may have moved; findings degrade to UNKNOWN.
TESTED_VERSIONS = ("2.1.",)

# $/MTok (input, output). Unknown models are excluded from cost, not guessed.
PRICES = {
    "claude-opus-5": (5.0, 25.0),
    "claude-opus-4-8": (5.0, 25.0),
    "claude-opus-4-7": (5.0, 25.0),
    "claude-opus-4-6": (5.0, 25.0),
    "claude-sonnet-5": (2.0, 10.0),
    "claude-sonnet-4-6": (3.0, 15.0),
    "claude-haiku-4-5": (1.0, 5.0),
    "claude-fable-5-1": (10.0, 50.0),
    "claude-fable-5": (10.0, 50.0),
}
CACHE_READ_MULT = 0.1
WRITE_MULT_5M = 1.25
WRITE_MULT_1H = 2.0
CHARS_PER_TOKEN = 4  # coarse; only used for on-disk text we cannot tokenize

# List-price estimates do not match real bills. Measured against one Enterprise
# account, this model ran 1.82x the actual billed amount - negotiated rates,
# plan discounts and billing details we cannot see all push the same way.
# So: dollars are INDICATIVE. What survives the error is the *ranking* of
# findings and their ratios to each other, because a uniform rate error scales
# every figure equally. Pass --actual-spend to calibrate against a real number.
CALIBRATION_NOTE = ("list-price estimate, typically higher than a real bill; "
                    "use --actual-spend to calibrate")

EXPENSIVE_MODELS = ("opus", "fable")


def price_for(model):
    if model in PRICES:
        return PRICES[model]
    base = re.sub(r"-\d{8}$", "", model or "")
    return PRICES.get(base)


def fmt_tok(n):
    if n >= 1_000_000:
        return f"{n / 1e6:.1f}M"
    if n >= 1000:
        return f"{n / 1000:.1f}k"
    return str(int(n))


def money(x):
    return f"${x:,.2f}"


class Finding:
    """One checklist row. status: PASS | FIX | N/A | UNKNOWN."""

    def __init__(self, cid, title, status, evidence="", why="", fix="",
                 can_fix="NONE", doc="", tokens_per_turn=0, monthly_usd=0.0,
                 payload=None):
        self.id = cid
        self.title = title
        self.status = status
        self.evidence = evidence
        self.why = why
        self.fix = fix
        self.can_fix = can_fix
        self.doc = doc
        self.tokens_per_turn = tokens_per_turn
        self.monthly_usd = monthly_usd
        self.payload = payload or {}

    def as_json(self):
        return {
            "id": self.id,
            "title": self.title,
            "status": self.status,
            "can_fix": self.can_fix,
            "tokens_per_turn": round(self.tokens_per_turn),
            "monthly_usd": round(self.monthly_usd, 2),
        }


DOCS = {
    "memory": "https://code.claude.com/docs/en/memory",
    "skills": "https://code.claude.com/docs/en/skills",
    "plugins": "https://code.claude.com/docs/en/discover-plugins",
    "mcp": "https://code.claude.com/docs/en/mcp",
    "model": "https://code.claude.com/docs/en/model-config",
    "subagents": "https://code.claude.com/docs/en/sub-agents",
    "context": "https://code.claude.com/docs/en/context-window",
    "settings": "https://code.claude.com/docs/en/settings",
}


# --------------------------------------------------------------------------
# environment
# --------------------------------------------------------------------------

def claude_version():
    exe = shutil.which("claude")
    if not exe:
        return None
    try:
        out = subprocess.run([exe, "--version"], capture_output=True, text=True,
                             timeout=15)
    except (OSError, subprocess.SubprocessError):
        return None
    m = re.search(r"\d+\.\d+\.\d+", (out.stdout or "") + (out.stderr or ""))
    return m.group(0) if m else None


def version_tested(v):
    return bool(v) and any(v.startswith(p) for p in TESTED_VERSIONS)


def read_json(path):
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return None


def load_settings(cwd):
    """Later entries win, mirroring Claude Code's precedence."""
    layers = [
        ("user", os.path.join(CLAUDE_DIR, "settings.json")),
        ("project", os.path.join(cwd, ".claude", "settings.json")),
        ("local", os.path.join(cwd, ".claude", "settings.local.json")),
    ]
    merged, sources = {}, {}
    for name, path in layers:
        data = read_json(path)
        if isinstance(data, dict):
            for k, v in data.items():
                merged[k] = v
                sources[k] = name
    return merged, sources


def mcp_servers():
    names = set()
    for path in (os.path.join(HOME, ".claude.json"),
                 os.path.join(CLAUDE_DIR, "settings.json"),
                 os.path.join(os.getcwd(), ".mcp.json")):
        data = read_json(path)
        if data is None:
            continue

        def walk(node):
            if isinstance(node, dict):
                for k, v in node.items():
                    if k == "mcpServers" and isinstance(v, dict):
                        names.update(v.keys())
                    else:
                        walk(v)
            elif isinstance(node, list):
                for item in node:
                    walk(item)

        walk(data)
    return names


# --------------------------------------------------------------------------
# config inventory
# --------------------------------------------------------------------------

FM_RE = re.compile(r"^---\n(.*?)\n---", re.S)


def parse_frontmatter(text):
    m = FM_RE.match(text)
    return m.group(1) if m else None


def fm_field(fm, name):
    m = re.search(rf"^{name}:\s*(.*(?:\n[ \t]+.*)*)", fm, re.M)
    return m.group(1).strip() if m else None


def scan_skills():
    """Inventory SKILL.md files. Personal ones are writable; plugin ones are not."""
    out = []
    roots = [(os.path.join(CLAUDE_DIR, "skills"), "personal"),
             (os.path.join(CLAUDE_DIR, "plugins"), "plugin")]
    for root, kind in roots:
        if not os.path.isdir(root):
            continue
        for path in glob.glob(os.path.join(root, "**", "SKILL.md"), recursive=True):
            try:
                with open(path, encoding="utf-8", errors="replace") as fh:
                    text = fh.read()
                mtime = os.path.getmtime(path)
            except OSError:
                continue
            fm = parse_frontmatter(text)
            if fm is None:
                continue
            name = fm_field(fm, "name") or os.path.basename(os.path.dirname(path))
            desc = fm_field(fm, "description") or ""
            disabled = bool(re.search(r"disable-model-invocation:\s*true", fm))
            plugin = None
            if kind == "plugin":
                rel = os.path.relpath(path, root).split(os.sep)
                plugin = rel[0] if rel else None
            out.append({
                "path": path, "kind": kind, "name": name, "plugin": plugin,
                "bytes": len(name) + len(desc), "disabled": disabled,
                "mtime": mtime,
            })
    return out


IMPORT_RE = re.compile(r"(?m)^\s*@([^\s`]+)\s*$")
FENCE_RE = re.compile(r"```.*?```", re.S)


def find_imports(text, base_dir):
    """Resolve @imports. Only count ones whose target exists on disk — bare
    regex matches decorators (@Injectable), npm scopes (@angular/core) and
    email addresses, which would otherwise be reported as findings."""
    stripped = FENCE_RE.sub("", text)
    found = []
    for raw in IMPORT_RE.findall(stripped):
        target = os.path.expanduser(raw)
        if not os.path.isabs(target):
            target = os.path.join(base_dir, target)
        if os.path.isfile(target):
            try:
                size = os.path.getsize(target)
            except OSError:
                size = 0
            found.append({"spec": raw, "path": target, "bytes": size})
    return found


def scan_claude_md(cwd):
    """Files that load EAGERLY: global, cwd, and every ancestor directory."""
    seen, entries = set(), []

    def add(path, role):
        real = os.path.realpath(path)
        if real in seen or not os.path.isfile(path):
            return
        seen.add(real)
        try:
            with open(path, encoding="utf-8", errors="replace") as fh:
                text = fh.read()
        except OSError:
            return
        entries.append({
            "path": path, "role": role, "bytes": len(text),
            "lines": text.count("\n") + 1 if text else 0,
            "imports": find_imports(text, os.path.dirname(path)),
        })

    add(os.path.join(CLAUDE_DIR, "CLAUDE.md"), "global")
    for base in (cwd, os.path.join(cwd, ".claude")):
        add(os.path.join(base, "CLAUDE.md"), "project")
        add(os.path.join(base, "CLAUDE.local.md"), "project")

    parent = os.path.dirname(os.path.abspath(cwd))
    while parent and parent != os.path.dirname(parent):
        if parent == HOME:
            add(os.path.join(parent, "CLAUDE.md"), "ancestor")
            break
        add(os.path.join(parent, "CLAUDE.md"), "ancestor")
        add(os.path.join(parent, ".claude", "CLAUDE.md"), "ancestor")
        parent = os.path.dirname(parent)
    return entries


# --------------------------------------------------------------------------
# transcripts (metadata only)
# --------------------------------------------------------------------------

class History:
    def __init__(self):
        self.turns = 0
        self.sessions = set()
        self.by_model = collections.defaultdict(collections.Counter)
        # Same shape, but only turns inside the user's billing period.
        self.cal_by_model = collections.defaultdict(collections.Counter)
        self.tool_calls = collections.Counter()
        self.skill_invocations = collections.Counter()
        self.sidechain_turns = 0
        self.subagent_turns = 0
        self.subagent_models = collections.Counter()
        self.mcp_used = set()
        self.denials = 0
        self.dup_reads = 0
        self.dup_read_bytes = 0
        self.earliest = None
        self.latest = None
        self.bad_lines = 0        # malformed JSON in a .jsonl transcript
        self.text_lines = 0       # expected plain text in mixed-format .output
        self.session_peak = {}    # session key -> peak context tokens
        self.cwds = set()         # exact project dirs, from transcripts
        self.missing_usage = 0
        self.write_ttl_known = 0
        self.write_ttl_unknown = 0

    @property
    def span_days(self):
        if not self.earliest or not self.latest:
            return None
        return max(1, (self.latest - self.earliest).days)


def _parse_ts(value):
    try:
        return dt.datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None


def transcript_paths():
    main = _dedupe(glob.glob(os.path.join(CLAUDE_DIR, "projects", "*", "*.jsonl")))
    # Subagent turns live in the session scratch tree and are billed too.
    # This location is ephemeral, so coverage is partial by nature.
    raw = []
    for base in glob.glob("/private/tmp/claude-*") + glob.glob("/tmp/claude-*"):
        raw.extend(glob.glob(os.path.join(base, "*", "*", "tasks", "*.output")))
    # /tmp is a symlink to /private/tmp on macOS, so the two globs return the
    # same files. Without realpath dedupe every subagent turn counts twice.
    return main, _dedupe(raw)


def _dedupe(paths):
    seen, out = set(), []
    for p in paths:
        real = os.path.realpath(p)
        if real not in seen:
            seen.add(real)
            out.append(p)
    return out


def _accumulate(counter, usage):
    """Add one turn's usage into a counter bucket."""
    counter["in"] += usage.get("input_tokens") or 0
    counter["out"] += usage.get("output_tokens") or 0
    counter["cache_read"] += usage.get("cache_read_input_tokens") or 0
    cc = usage.get("cache_creation")
    if isinstance(cc, dict):
        counter["w5m"] += cc.get("ephemeral_5m_input_tokens") or 0
        counter["w1h"] += cc.get("ephemeral_1h_input_tokens") or 0
    else:
        counter["w_unknown"] += usage.get("cache_creation_input_tokens") or 0
    counter["turns"] += 1


def scan_history(window_days, cal_since=None, cal_until=None):
    cutoff = dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=window_days)
    h = History()
    main, sub = transcript_paths()

    for path in main + sub:
        is_sub = path.endswith(".output")
        reads = collections.Counter()
        session_id = os.path.basename(path).rsplit(".", 1)[0]
        for line in _iter_lines(path, h, is_sub):
            entry = line
            if not isinstance(entry, dict):
                continue

            ts = _parse_ts(entry.get("timestamp"))
            if ts:
                h.earliest = min(h.earliest or ts, ts)
                h.latest = max(h.latest or ts, ts)
            # Exact working directory, recorded per entry. Far better than
            # decoding project slugs, where a directory name containing a
            # space or dash cannot be reconstructed.
            cwd_value = entry.get("cwd")
            if isinstance(cwd_value, str) and cwd_value.startswith("/"):
                h.cwds.add(cwd_value)

            msg = entry.get("message")
            if isinstance(msg, dict):
                usage = msg.get("usage")
                model = msg.get("model") or "unknown"
                if isinstance(usage, dict):
                    if ts and ts < cutoff:
                        pass  # outside window: counted only for span
                    else:
                        c = h.by_model[model]
                        c["in"] += usage.get("input_tokens") or 0
                        c["out"] += usage.get("output_tokens") or 0
                        c["cache_read"] += usage.get("cache_read_input_tokens") or 0
                        cc = usage.get("cache_creation")
                        if isinstance(cc, dict):
                            c["w5m"] += cc.get("ephemeral_5m_input_tokens") or 0
                            c["w1h"] += cc.get("ephemeral_1h_input_tokens") or 0
                            h.write_ttl_known += 1
                        else:
                            c["w_unknown"] += usage.get("cache_creation_input_tokens") or 0
                            h.write_ttl_unknown += 1
                        c["turns"] += 1
                        h.turns += 1
                        key = (path, session_id)
                        # Calibration must cover exactly the period the user's
                        # billed figure covers - bounded at BOTH ends. Without
                        # an upper bound, usage accrued after they read their
                        # bill inflates the denominator and the factor decays
                        # silently on every later run.
                        if (cal_since is not None and ts is not None
                                and ts >= cal_since
                                and (cal_until is None or ts <= cal_until)):
                            _accumulate(h.cal_by_model[model], usage)
                        h.sessions.add(key)
                        ctx = ((usage.get("input_tokens") or 0)
                               + (usage.get("cache_read_input_tokens") or 0))
                        if ctx > h.session_peak.get(key, 0):
                            h.session_peak[key] = ctx
                        if is_sub:
                            h.subagent_turns += 1
                            h.subagent_models[model] += 1
                        elif entry.get("isSidechain"):
                            h.sidechain_turns += 1
                elif msg.get("role") == "assistant":
                    h.missing_usage += 1

                content = msg.get("content")
                if isinstance(content, list) and not (ts and ts < cutoff):
                    for block in content:
                        if not isinstance(block, dict) or block.get("type") != "tool_use":
                            continue
                        name = block.get("name") or "?"
                        h.tool_calls[name] += 1
                        if name.startswith("mcp__"):
                            parts = name.split("__")
                            if len(parts) > 1:
                                h.mcp_used.add(parts[1])
                        inp = block.get("input")
                        if not isinstance(inp, dict):
                            continue
                        if name == "Skill" and inp.get("skill"):
                            h.skill_invocations[str(inp["skill"])] += 1
                        if name == "Read" and inp.get("file_path"):
                            key = str(inp["file_path"])
                            try:
                                reads["__last_size__"] = os.path.getsize(key)
                            except OSError:
                                reads["__last_size__"] = 0
                            reads[key] += 1
                            if reads[key] > 1:
                                h.dup_reads += 1
                                h.dup_read_bytes += reads.get("__last_size__", 0)

            if isinstance(msg, dict) and not (ts and ts < cutoff):
                # A skill can also be invoked as /name, which never produces a
                # Skill tool_use. Missing these makes A4/A5 accuse the user of
                # hoarding skills they actually use - and A4 is an AUTO fix.
                for name in _slash_commands(msg):
                    h.skill_invocations[name] += 1

            if entry.get("type") == "user" and _looks_denied(entry):
                h.denials += 1
        del reads
    return h


def _iter_lines(path, h, is_sub=False):
    """Yield parsed JSON objects. Non-JSON lines are discarded without being
    inspected: subagent .output files are mixed-format and carry plain
    conversation text, which this tool must never read."""
    try:
        fh = open(path, encoding="utf-8", errors="replace")
    except OSError:
        return
    with fh:
        for raw in fh:
            raw = raw.strip()
            if not raw:
                continue
            if not (raw[0] in "{["):
                # Cheap structural reject - avoids treating prose as corruption.
                if is_sub:
                    h.text_lines += 1
                else:
                    h.bad_lines += 1
                continue
            try:
                yield json.loads(raw)
            except ValueError:
                if is_sub:
                    h.text_lines += 1
                else:
                    h.bad_lines += 1


CMD_RE = re.compile(r"<command-name>/?([\w:-]+)</command-name>")


def _slash_commands(msg):
    """Skill names invoked as /name. Claude Code records these as text in the
    user message, not as a Skill tool_use, so they need separate detection."""
    found = []
    content = msg.get("content")
    if isinstance(content, str):
        found += CMD_RE.findall(content)
    elif isinstance(content, list):
        for block in content:
            if isinstance(block, dict) and block.get("type") == "text":
                found += CMD_RE.findall(block.get("text") or "")
    return found


DENY_RE = re.compile(r"user (?:doesn't|does not) want to (?:take this action|proceed)"
                     r"|operation (?:was )?(?:rejected|denied)"
                     r"|permission (?:to use|denied)", re.I)


def _looks_denied(entry):
    msg = entry.get("message")
    if not isinstance(msg, dict):
        return False
    content = msg.get("content")
    texts = []
    if isinstance(content, str):
        texts.append(content)
    elif isinstance(content, list):
        for block in content:
            if isinstance(block, dict):
                c = block.get("content")
                if isinstance(c, str):
                    texts.append(c)
    return any(DENY_RE.search(t) for t in texts)


# --------------------------------------------------------------------------
# cost model
# --------------------------------------------------------------------------

def model_cost(model, c):
    p = price_for(model)
    if not p:
        return None
    pin, pout = p
    writes = (c["w5m"] * WRITE_MULT_5M + c["w1h"] * WRITE_MULT_1H
              + c["w_unknown"] * WRITE_MULT_1H)
    return (c["in"] * pin + c["out"] * pout
            + c["cache_read"] * pin * CACHE_READ_MULT
            + writes * pin) / 1e6


def blended_input_rate(h):
    """Weighted input $/MTok across observed models, for overhead pricing."""
    num = den = 0.0
    for model, c in h.by_model.items():
        p = price_for(model)
        if not p:
            continue
        num += p[0] * c["turns"]
        den += c["turns"]
    return (num / den) if den else 5.0


def overhead_monthly_usd(tokens_per_turn, h, write_mult):
    """The cache-rate rule: always-on overhead sits in the cached prefix, so it
    bills at ~0.1x input per turn plus one write per session. Using the raw
    input rate here overstates savings by roughly 7-10x."""
    rate = blended_input_rate(h)
    reads = tokens_per_turn * h.turns * CACHE_READ_MULT * rate / 1e6
    writes = tokens_per_turn * max(len(h.sessions), 1) * write_mult * rate / 1e6
    return reads + writes


def dominant_write_mult(h):
    if h.write_ttl_known == 0 and h.write_ttl_unknown == 0:
        return WRITE_MULT_1H
    w5 = sum(c["w5m"] for c in h.by_model.values())
    w1 = sum(c["w1h"] for c in h.by_model.values()) + \
        sum(c["w_unknown"] for c in h.by_model.values())
    return WRITE_MULT_5M if w5 > w1 else WRITE_MULT_1H


# --------------------------------------------------------------------------
# checks
# --------------------------------------------------------------------------

def unused_window(h, window_days, mtime):
    """min(window, age of the item, span of available history). Anything newer
    than the evidence window cannot be called unused."""
    age_days = (dt.datetime.now().timestamp() - mtime) / 86400
    candidates = [window_days, age_days]
    if h.span_days:
        candidates.append(h.span_days)
    return min(candidates)


def other_project_claude_md(cwd, known_cwds, limit=40):
    """CLAUDE.md files in the user's OTHER project directories.

    A1/A2 only see the current working directory's chain, so running the audit
    from an unrelated folder would report "nothing here" while large files sit
    elsewhere. Directories come from the `cwd` field recorded in transcripts -
    exact, unlike decoding project slugs, where a directory name containing a
    space or a dash cannot be reconstructed."""
    here = os.path.realpath(cwd)
    seen, found = set(), []
    for path in known_cwds:
        if not os.path.isdir(path) or os.path.realpath(path) == here:
            continue
        for candidate in (os.path.join(path, "CLAUDE.md"),
                          os.path.join(path, ".claude", "CLAUDE.md")):
            real = os.path.realpath(candidate)
            if real in seen or not os.path.isfile(candidate):
                continue
            seen.add(real)
            try:
                size = os.path.getsize(candidate)
                with open(candidate, encoding="utf-8", errors="replace") as fh:
                    lines = fh.read().count("\n") + 1
            except OSError:
                continue
            if size:
                found.append({"path": candidate, "bytes": size, "lines": lines})
    found.sort(key=lambda e: -e["bytes"])
    return found[:limit]


def check_claude_md(entries, h, wmult, elsewhere=None):
    findings = []
    eager = [e for e in entries if e["role"] in ("global", "project")]
    # A1 is a machine-wide inventory: every project's CLAUDE.md is always-on
    # overhead *in that project*, so reporting only the current directory hides
    # most of the cost. A2 stays cwd-relative because ancestor stacking is
    # inherently a property of where you are.
    everywhere = list(eager) + list(elsewhere or [])
    biggest = max(everywhere, key=lambda e: e["lines"], default=None)
    over = [e for e in everywhere if e["lines"] > 200]
    if over:
        tok = sum(e["bytes"] for e in over) / CHARS_PER_TOKEN
        findings.append(Finding(
            "A1", "Long instruction files", "FIX",
            evidence=f"{len(over)} of {len(everywhere)} files over 200 lines: "
                     + "; ".join(f"{_short(e['path'])} ({e['lines']})"
                                 for e in over[:5]),
            why="CLAUDE.md is your instructions file for Claude. Claude re-reads "
                "it on every single message, so every line in it costs you money "
                "all day long. Two of them are read this way: the one in your "
                "project folder, and your global one. A CLAUDE.md inside a "
                "sub-folder is different - Claude only reads that one if it "
                "opens a file in that sub-folder. Aim for under 200 lines each.",
            fix="Take the rules that only matter inside one sub-folder and move "
                "them into a CLAUDE.md in that sub-folder. Delete anything "
                "out of date.",
            can_fix="ASSISTED", doc=DOCS["memory"],
            tokens_per_turn=tok, monthly_usd=overhead_monthly_usd(tok, h, wmult)))
    elif not everywhere:
        findings.append(Finding("A1", "Long instruction files", "N/A",
                                evidence="you do not have any CLAUDE.md instruction files yet"))
    else:
        n = biggest["lines"] if biggest else 0
        total_tok = sum(e["bytes"] for e in everywhere) / CHARS_PER_TOKEN
        findings.append(Finding(
            "A1", "Long instruction files", "PASS",
            evidence=f"{len(everywhere)} file(s) across your projects, largest "
                     f"{n} lines - all under the 200-line guideline "
                     f"(~{int(total_tok)} tokens in total)"))

    anc = [e for e in entries if e["role"] == "ancestor"]
    if anc:
        tok = sum(e["bytes"] for e in anc) / CHARS_PER_TOKEN
        findings.append(Finding(
            "A2", "Instruction files stacking up", "FIX",
            evidence="; ".join(f"{_short(e['path'])} ~{int(e['bytes']/CHARS_PER_TOKEN)} tok"
                               for e in anc),
            why="Claude reads the CLAUDE.md in the folder you are working in, "
                "plus the one in every folder above it, and adds them all "
                "together. So a big instructions file sitting near the top of "
                "your drive gets paid for in every project underneath it, on "
                "every message.",
            fix="Move rules that belong to one project down into that project's "
                "folder. In the folders above, keep only the rules you genuinely "
                "want applied everywhere.",
            can_fix="ASSISTED", doc=DOCS["memory"],
            tokens_per_turn=tok, monthly_usd=overhead_monthly_usd(tok, h, wmult)))
    else:
        findings.append(Finding("A2", "Instruction files stacking up", "PASS",
                                evidence="no instruction files in the folders above this one"))

    imports = [(e, i) for e in entries for i in e["imports"]]
    if imports:
        tok = sum(i["bytes"] for _, i in imports) / CHARS_PER_TOKEN
        findings.append(Finding(
            "A3", "Imported files in CLAUDE.md", "FIX",
            evidence="; ".join(f"{_short(e['path'])} -> @{i['spec']} "
                               f"({i['bytes']}B)" for e, i in imports),
            why="A line starting with @ inside CLAUDE.md pulls in another file's "
                "whole contents. That happens when the session starts, not when "
                "the file is needed - so breaking a long CLAUDE.md into imports "
                "does not save you anything. The only thing that loads on demand "
                "is a CLAUDE.md inside a sub-folder.",
            fix="If the imported file is about one sub-folder, move it there and "
                "rename it CLAUDE.md. Otherwise paste the content in or drop it.",
            can_fix="ASSISTED", doc=DOCS["memory"],
            tokens_per_turn=tok, monthly_usd=overhead_monthly_usd(tok, h, wmult)))
    else:
        findings.append(Finding("A3", "Imported files in CLAUDE.md", "PASS",
                                evidence="no @ imports in your always-loaded files"))
    return findings


def _short(path):
    return path.replace(HOME, "~")


def _invoked(name, plugin, invocations):
    if invocations.get(name):
        return True
    if plugin and invocations.get(f"{plugin}:{name}"):
        return True
    return any(k.endswith(f":{name}") for k in invocations)


def check_skills(skills, h, window_days, wmult, enabled_plugins):
    findings = []
    personal = [s for s in skills if s["kind"] == "personal" and not s["disabled"]]
    stale = []
    for s in personal:
        win = unused_window(h, window_days, s["mtime"])
        if win < 7:
            continue  # too new to judge
        if not _invoked(s["name"], None, h.skill_invocations):
            stale.append(s)

    if not h.turns:
        findings.append(Finding("A4", "Skills you never use", "UNKNOWN",
                                evidence="no session history to look at yet"))
    elif stale:
        tok = sum(s["bytes"] for s in stale) / CHARS_PER_TOKEN
        findings.append(Finding(
            "A4", "Skills you never use", "FIX",
            evidence=f"{len(stale)} skills you have not used in {window_days} days: "
                     + ", ".join(s["name"] for s in stale[:8]),
            why="So Claude can pick a skill on its own, it keeps every skill's "
                "name and description in front of it on every message - "
                "including the ones you never use. One line takes a skill off "
                "that list without removing it: you can still run it by typing a "
                "slash and its name.",
            fix="Add the line 'disable-model-invocation: true' at the top of each "
                "unused skill's file. Nothing is deleted, and you can still run "
                "each one by name.",
            can_fix="AUTO", doc=DOCS["skills"],
            tokens_per_turn=tok, monthly_usd=overhead_monthly_usd(tok, h, wmult),
            payload={"paths": [s["path"] for s in stale]}))
    else:
        findings.append(Finding("A4", "Skills you never use", "PASS",
                                evidence=f"{len(personal)} of your own skills - all either used "
                                         "recently or already off the list"))

    by_plugin = collections.defaultdict(list)
    for s in skills:
        if s["kind"] == "plugin" and s["plugin"]:
            by_plugin[s["plugin"]].append(s)

    active = {k.split("@")[0]: v for k, v in (enabled_plugins or {}).items()}
    idle = []
    for plugin, group in by_plugin.items():
        if active.get(plugin) is False:
            continue
        newest = max(s["mtime"] for s in group)
        if unused_window(h, window_days, newest) < 7:
            continue
        if any(_invoked(s["name"], plugin, h.skill_invocations) for s in group):
            continue
        idle.append((plugin, group))

    if not h.turns:
        findings.append(Finding("A5", "Plugins you never use", "UNKNOWN",
                                evidence="no session history to look at yet"))
    elif idle:
        tok = sum(s["bytes"] for _, g in idle for s in g) / CHARS_PER_TOKEN
        names = [p for p, _ in idle]
        findings.append(Finding(
            "A5", "Plugins you never use", "FIX",
            evidence=f"not used in the last {window_days} days: "
                     + "; ".join(f"{p} ({len(g)} skills)" for p, g in idle),
            why="A plugin is a bundle of skills that someone packaged together. "
                "Every skill inside it adds its description to the list Claude "
                "carries on every message. You cannot switch off one skill inside "
                "a plugin, so the whole plugin is the only on/off switch. Turning "
                "it off does not delete it - you can turn it back on any time.",
            fix="Type this in Claude Code: "
                + "; ".join(f"/plugin disable {p}" for p in names[:4]),
            can_fix="AUTO", doc=DOCS["plugins"],
            tokens_per_turn=tok, monthly_usd=overhead_monthly_usd(tok, h, wmult),
            payload={"plugins": names, "enabled_keys": list(enabled_plugins or {})}))
    else:
        findings.append(Finding("A5", "Plugins you never use", "PASS",
                                evidence=f"{len(by_plugin)} plugins, all either used or already off"))
    return findings


CLI_ALTERNATIVES = {
    "github": "gh", "atlassian": "acli", "jira": "acli",
    "confluence": "acli", "gitlab": "glab",
}



MEMORY_LINK_RE = re.compile(r"\[[^\]]*\]\(([^)]+\.md)\)")
MEMORY_LINE_LIMIT = 150   # guidance: index entries stay one short line
MEMORY_LINE_COUNT = 200   # lines past this are truncated


def check_memory(cwd, h, wmult):
    """Auto-memory overhead, machine-wide.

    MEMORY.md is loaded on every turn of its project, so each one is always-on
    overhead in that project. Reporting only the current project hides the rest.
    Individual memory files load on demand and are NOT per-turn cost - never
    price them as if they were."""
    dirs = sorted(glob.glob(os.path.join(CLAUDE_DIR, "projects", "*", "memory")))
    if not dirs:
        return [Finding("A7", "Memory notes too long", "N/A",
                        evidence="no memory directories found")]

    total_tok = 0.0
    problems, indexes = [], 0
    worst_tok = 0.0
    for mem_dir in dirs:
        project = os.path.basename(os.path.dirname(mem_dir))
        index = os.path.join(mem_dir, "MEMORY.md")
        files = [q for q in glob.glob(os.path.join(mem_dir, "*.md"))
                 if os.path.basename(q) != "MEMORY.md"]
        if not os.path.isfile(index):
            if files:
                problems.append(f"{_tail(project)}: {len(files)} memory file(s) "
                                "with no MEMORY.md index (never surfaced)")
            continue
        try:
            with open(index, encoding="utf-8", errors="replace") as fh:
                text = fh.read()
        except OSError:
            continue
        if not text.strip():
            continue
        indexes += 1
        tok = len(text) / CHARS_PER_TOKEN
        total_tok += tok
        worst_tok = max(worst_tok, tok)
        lines = [ln for ln in text.splitlines() if ln.strip()]
        linked = {os.path.basename(t) for t in MEMORY_LINK_RE.findall(text)}
        orphans = [os.path.basename(q) for q in files
                   if os.path.basename(q) not in linked]
        broken = [t for t in linked
                  if not os.path.isfile(os.path.join(mem_dir, t))]
        long_lines = [ln for ln in lines
                      if ln.startswith("-") and len(ln) > MEMORY_LINE_LIMIT]
        bits = []
        if len(lines) > MEMORY_LINE_COUNT:
            bits.append(f"{len(lines)} lines (past {MEMORY_LINE_COUNT} is truncated)")
        if long_lines:
            bits.append(f"{len(long_lines)} line(s) too long")
        if orphans:
            bits.append(f"{len(orphans)} memory file(s) missing from the list")
        if broken:
            bits.append(f"{len(broken)} link(s) pointing at a deleted file")
        if bits:
            problems.append(f"{_tail(project)} (~{int(tok)} tokens per message): "
                            + ", ".join(bits))

    if not indexes and not problems:
        return [Finding("A7", "Memory notes too long", "N/A",
                        evidence="no memories stored")]
    if not problems:
        return [Finding(
            "A7", "Memory notes too long", "PASS",
            evidence=f"{indexes} MEMORY.md file(s), largest ~{int(worst_tok)} "
                     f"tokens per message in its project, all entries indexed and short")]
    # Price only the worst offender: each MEMORY.md is per-turn cost in its own
    # project, so summing across projects would double-count a cost the user
    # never pays simultaneously.
    return [Finding(
        "A7", "Memory notes too long", "FIX",
        evidence=f"{len(problems)} of your {indexes} project memory lists need tidying - "
                 + "; ".join(problems[:4]),
        why="MEMORY.md is the contents page of what Claude remembers about a "
            "project, and it loads on every message there. It should be one "
            "short line per memory - the detail belongs in the files it links "
            "to, which load only when relevant. Two traps: a memory file missing "
            "from the list is never used at all, and a very long list gets cut "
            "off at the end.",
        fix="Shorten each line to one sentence and move the detail into the file "
            "it links to. Add any memory file that is missing from the list, and "
            "remove links pointing at files that no longer exist.",
        can_fix="ASSISTED", doc=DOCS["memory"],
        tokens_per_turn=worst_tok,
        monthly_usd=overhead_monthly_usd(worst_tok, h, wmult))]


def _tail(text, n=26):
    return text[-n:]


def _slug_matches(mem_dir, cwd):
    """A project slug is the cwd with separators replaced. Compare on the
    alphanumeric tail, since '-' vs ' ' vs '/' cannot be reversed reliably."""
    slug = os.path.basename(os.path.dirname(mem_dir))
    norm = lambda x: re.sub(r"[^a-z0-9]", "", x.lower())
    return norm(slug) == norm(cwd)


def mcp_name_overhead(h, unused_count):
    """Estimate always-loaded tool-name cost for idle MCP servers.

    We cannot enumerate an unused server's tools without connecting to it. But
    tool names of *used* servers appear in transcripts, so their observed
    average (tools per server, chars per name) extrapolates to the idle ones.
    An estimate with a stated basis beats 'not quantified'."""
    per_server = collections.defaultdict(set)
    for name in h.tool_calls:
        if name.startswith("mcp__"):
            parts = name.split("__")
            if len(parts) > 2:
                per_server[parts[1]].add(parts[2])
    if not per_server:
        return None
    tools_per = sum(len(v) for v in per_server.values()) / len(per_server)
    chars_per = (sum(len(t) for v in per_server.values() for t in v)
                 / max(sum(len(v) for v in per_server.values()), 1))
    # Observed counts are a floor: only tools actually called are visible.
    tok = unused_count * tools_per * (chars_per / CHARS_PER_TOKEN)
    return {"tokens": tok, "tools_per_server": round(tools_per, 1),
            "chars_per_name": round(chars_per, 1)}


def check_mcp(configured, h, window_days=30, wmult=WRITE_MULT_1H):
    if not configured:
        return [Finding("A6", "Connected tools you never use", "N/A", evidence="you have no outside services connected")]
    if not h.turns:
        return [Finding("A6", "Connected tools you never use", "UNKNOWN",
                        evidence="no session history to look at yet")]
    unused = sorted(s for s in configured if s not in h.mcp_used)
    if not unused:
        return [Finding("A6", "Connected tools you never use", "PASS",
                        evidence=f"all {len(configured)} connected services were actually used")]
    est = mcp_name_overhead(h, len(unused))
    swaps = [f"{s} -> {CLI_ALTERNATIVES[k]}" for s in unused
             for k in CLI_ALTERNATIVES if k in s.lower()]
    fix = ("Disconnect the servers you are not using. You can always reconnect "
           "one later if you need it.")
    if swaps:
        fix += (" These also have a command-line tool that does the same job for "
                "free, with no always-on cost: " + ", ".join(sorted(set(swaps))))
    return [Finding(
        "A6", f"Connected tools not used in {window_days}d", "FIX",
        evidence=f"{len(unused)} of {len(configured)} unused: "
                 + ", ".join(unused[:8])
                 + (f" (~{fmt_tok(est['tokens'])} tokens per message est.)" if est else ""),
        why="An MCP server is an outside service you connect to Claude, like "
            "Gmail or Jira. Its tool instructions only load when needed now, but "
            "the tool names still sit in the list Claude carries on every "
            "message. Anthropic does not publish that cost, so this is our own "
            "estimate from the servers you do use"
            + (f" ({est['tools_per_server']} tools each, "
               f"{est['chars_per_name']} characters per name) - a floor, not a "
               "measurement." if est else "."),
        fix=fix, can_fix="MANUAL", doc=DOCS["mcp"],
        tokens_per_turn=est["tokens"] if est else 0,
        monthly_usd=(overhead_monthly_usd(est["tokens"], h, wmult) if est else 0.0),
        payload={"unused": unused})]


def check_models(h, settings, sources):
    findings = []
    if not h.turns:
        return [Finding("B1", "Expensive model on easy work", "UNKNOWN",
                        evidence="no session history to look at yet"),
                Finding("B2", "Helpers using the expensive model", "UNKNOWN", evidence="no session history to look at yet"),
                Finding("B3", "Thinking effort set to high", "UNKNOWN", evidence="no session history to look at yet")]

    costs, exp_turns, total_turns = {}, 0, 0
    for model, c in h.by_model.items():
        k = model_cost(model, c)
        if k is not None:
            costs[model] = k
        total_turns += c["turns"]
        if any(tag in model for tag in EXPENSIVE_MODELS):
            exp_turns += c["turns"]
    share = (exp_turns / total_turns) if total_turns else 0
    exp_cost = sum(v for m, v in costs.items()
                   if any(t in m for t in EXPENSIVE_MODELS))
    sonnet_in, sonnet_out = PRICES["claude-sonnet-5"]
    alt = 0.0
    for model, c in h.by_model.items():
        if any(t in model for t in EXPENSIVE_MODELS):
            writes = (c["w5m"] * WRITE_MULT_5M + c["w1h"] * WRITE_MULT_1H
                      + c["w_unknown"] * WRITE_MULT_1H)
            alt += (c["in"] * sonnet_in + c["out"] * sonnet_out
                    + c["cache_read"] * sonnet_in * CACHE_READ_MULT
                    + writes * sonnet_in) / 1e6

    model_setting = settings.get("model")
    if share > 0.4 and exp_cost > alt:
        half = (exp_cost - alt) / 2
        where = f" (set in {sources.get('model', '?')} settings)" if model_setting else ""
        findings.append(Finding(
            "B1", "Expensive model on easy work", "FIX",
            evidence=f"{share*100:.0f}% of your messages went to an expensive model, costing "
                     f"{money(exp_cost)} of {money(sum(costs.values()))} in this period"
                     + (f"; model={model_setting}{where}" if model_setting else ""),
            why="Claude Code fixes the model when a session starts and never "
                "switches by itself, so your priciest model also handles 'rename "
                "this variable'. The same work at Sonnet's rates would have cost "
                + money(alt) + " - a best case, since it assumes Sonnet could "
                "have finished it. Switch at the start of a session, not "
                "mid-way: changing model discards the cached conversation, so "
                "all of it gets re-sent once at full price.",
            fix=("Type /model sonnet at the start of a session you know is easy"
                 + (", or change your default `model` setting. "
                    if model_setting else ". ")
                 + "You can also turn on a reminder that speaks up on your first "
                   "message, and only when the task clearly does not need what "
                   f'you are paying for: `python3 "{SELF_PATH}" --install-hook`'),
            can_fix="ASSISTED", doc=DOCS["model"], monthly_usd=half,
            payload={"model_setting": model_setting,
                     "upper_bound_usd": round(exp_cost - alt, 2),
                     "action": "install_hook"}))
    else:
        findings.append(Finding("B1", "Expensive model on easy work", "PASS",
                                evidence=f"only {share*100:.0f}% of messages used an expensive model"))

    sub_env = os.environ.get("CLAUDE_CODE_SUBAGENT_MODEL")
    if h.subagent_turns == 0:
        findings.append(Finding("B2", "Helpers using the expensive model", "N/A",
                                evidence="you did not use any helper subagents in this period"))
    else:
        pricey = sum(n for m, n in h.subagent_models.items()
                     if any(t in m for t in EXPENSIVE_MODELS))
        if pricey and not sub_env:
            findings.append(Finding(
                "B2", "Helpers using the expensive model", "FIX",
                evidence=f"{pricey} of {h.subagent_turns} helper tasks ran on an expensive model",
                why="A subagent is a helper Claude. It goes off, does one job - "
                    "usually searching or reading a lot of files - and comes back "
                    "with just the answer. Helpers use the same model as your "
                    "main session unless you tell them not to, and searching "
                    "rarely needs the top model.",
                fix="Point helpers at a cheaper model by setting "
                    "CLAUDE_CODE_SUBAGENT_MODEL=claude-sonnet-5 (or "
                    "claude-haiku-4-5, which is cheaper still). Your main "
                    "session is not affected.",
                can_fix="AUTO", doc=DOCS["subagents"],
                payload={"env": "CLAUDE_CODE_SUBAGENT_MODEL"}))
        else:
            findings.append(Finding(
                "B2", "Helpers using the expensive model", "PASS",
                evidence=f"{h.subagent_turns} helper tasks; "
                         + (f"env set to {sub_env}" if sub_env
                            else "already using cheaper models")))

    effort = settings.get("effortLevel")
    if effort in ("high", "xhigh", "max"):
        # Effort acts on reasoning/output tokens, so the output-token spend is
        # the ceiling on what lowering it can save. Quoted as a bound, with the
        # assumed reduction stated - not as a promise.
        out_cost = 0.0
        for model, c in h.by_model.items():
            pr = price_for(model)
            if pr:
                out_cost += c["out"] * pr[1] / 1e6
        assumed_cut = 0.30
        findings.append(Finding(
            "B3", "Thinking effort set to high", "FIX",
            evidence=f"effort is set to '{effort}' for every session"
                     + f" (in your {sources.get('effortLevel','?')} settings)",
            why="Effort is how long Claude thinks before answering, and you pay "
                "for that thinking. It is set once per session, so a high "
                "default applies to simple jobs too. Dropping effort usually "
                "costs less quality than dropping to a weaker model, so try it "
                "first. The figure assumes a 30% cut on routine work; thinking "
                "and answers cost " + money(out_cost) + " in total here, which "
                "is the ceiling.",
            fix="Set your default effort to medium, and raise it for the "
                "occasional hard task that needs it.",
            can_fix="ASSISTED", doc=DOCS["settings"],
            monthly_usd=out_cost * assumed_cut,
            payload={"effortLevel": effort,
                     "output_cost_ceiling_usd": round(out_cost, 2),
                     "assumed_reduction": assumed_cut}))
    elif effort:
        findings.append(Finding("B3", "Thinking effort set to high", "PASS",
                                evidence=f"effort is set to '{effort}', which is not a costly default"))
    else:
        findings.append(Finding("B3", "Thinking effort set to high", "PASS",
                                evidence="you have not forced a high effort default"))
    return findings


def check_cache(h, settings):
    findings = []
    reads = sum(c["cache_read"] for c in h.by_model.values())
    writes = sum(c["w5m"] + c["w1h"] + c["w_unknown"] for c in h.by_model.values())
    fresh = sum(c["in"] for c in h.by_model.values())
    readable = reads + writes + fresh
    if not readable:
        findings.append(Finding("C1", "Paying twice for the same text", "UNKNOWN",
                                evidence="no usage figures in this period"))
        findings.append(Finding("C2", "Conversation trimming", "UNKNOWN", evidence="skipped - could not measure text re-use"))
        return findings

    rate = reads / readable
    if rate >= 0.8:
        findings.append(Finding(
            "C1", "Paying twice for the same text", "PASS",
            evidence=f"{rate*100:.1f}% of your text was re-used from cache at a tenth of the price"))
        findings.append(Finding(
            "C2", "Conversation trimming", "N/A",
            evidence="skipped - your text is being re-used well already, so changing this would not help"))
        return findings

    findings.append(Finding(
        "C1", "Paying twice for the same text", "FIX",
        evidence=f"{rate*100:.1f}% cache hits ({fmt_tok(reads)} read vs "
                 f"{fmt_tok(writes)} written, {fmt_tok(fresh)} uncached)",
        why="Claude re-uses the conversation so far at about a tenth of the "
            "normal price - that is the cache. When it misses you pay full price "
            "again for text you already paid for, usually because CLAUDE.md or "
            "your settings changed mid-session. One warning: shrinking your "
            "context in a way that breaks the cache can cost more, not less.",
        fix="Try not to edit CLAUDE.md or your settings mid-session - finish the "
            "session first. Put the things that do not change at the start of "
            "the conversation.",
        can_fix="NONE", doc=DOCS["context"]))

    win = settings.get("autoCompactWindow")
    enabled = settings.get("autoCompactEnabled")
    findings.append(Finding(
        "C2", "Conversation trimming", "FIX",
        evidence=f"autoCompactEnabled={enabled}, autoCompactWindow={win}",
        why="When a conversation gets long, Claude Code summarises the older "
            "part to make room. That is called compacting. Because you are "
            "already paying twice for a lot of text (see the check above), "
            "summarising sooner leaves less text to re-send each time.",
        fix="Have a look at your autoCompactEnabled and autoCompactWindow "
            "settings, and try summarising earlier than the default.",
        can_fix="AUTO", doc=DOCS["settings"],
        payload={"autoCompactWindow": win}))
    return findings


def check_habits(h):
    findings = []
    if not h.turns:
        return [Finding("D1", "Big searches done in the main chat", "UNKNOWN", evidence="no session history to look at yet")]

    total = h.turns
    sub_share = (h.subagent_turns + h.sidechain_turns) / total
    if sub_share < 0.02:
        findings.append(Finding(
            "D1", "Big searches done in the main chat", "FIX",
            evidence=f"{h.subagent_turns + h.sidechain_turns} of {total} turns "
                     f"({sub_share*100:.1f}%) were handled by a helper",
            why="When Claude searches your files in the main conversation, every "
                "raw result stays in that conversation - and you pay for all of "
                "it again on every message that follows. A subagent (a helper "
                "Claude) does the same search in its own separate conversation "
                "and hands back just the answer, so the noise never lands in "
                "yours.",
            fix="For anything that means digging through a lot of files, ask "
                "Claude to use a subagent - for example: 'use a subagent to find "
                "where X is defined'.",
            can_fix="NONE", doc=DOCS["subagents"]))
    else:
        findings.append(Finding("D1", "Big searches done in the main chat", "PASS",
                                evidence=f"{sub_share*100:.1f}% of work handed to a helper, which is healthy"))

    shots = sum(n for t, n in h.tool_calls.items()
                if "computer" in t or "screenshot" in t)
    if shots > max(20, total * 0.05):
        # Screenshots are billed as images. ~1.5k tokens is a working figure
        # for a full-window capture; stated so the reader can discount it.
        per_image = 1500
        img_tok = shots * per_image
        img_cost = img_tok * blended_input_rate(h) / 1e6
        findings.append(Finding(
            "D2", "Lots of screenshots", "FIX",
            evidence=f"{shots} screenshot/computer-use calls "
                     f"(~{fmt_tok(img_tok)} image tokens at ~{per_image}/image)",
            monthly_usd=img_cost,
            why="A screenshot goes into the conversation as a picture. One "
                "picture costs far more than a line of text, and it stays in the "
                "conversation from then on, so you keep paying for it. We "
                "estimate about 1,500 tokens per screenshot - that is a rule of "
                "thumb, not a measurement, because the exact count per image is "
                "not recorded.",
            fix="When you only need to know what a page says, ask Claude to read "
                "the page text or the error log instead of taking a picture. Keep "
                "screenshots for when you actually need to see the layout.",
            can_fix="NONE", doc=DOCS["context"]))
    else:
        findings.append(Finding("D2", "Lots of screenshots", "PASS",
                                evidence=f"{shots} screenshots - not enough to matter"))

    if h.dup_reads > max(20, total * 0.05):
        # Each repeat read adds another copy to context, then rides along in
        # the cached prefix for the rest of the session.
        dup_tok = h.dup_read_bytes / CHARS_PER_TOKEN if h.dup_read_bytes else 0
        dup_cost = dup_tok * blended_input_rate(h) / 1e6 if dup_tok else 0.0
        findings.append(Finding(
            "D3", "Reading the same file twice", "FIX",
            evidence=f"{h.dup_reads} files were read again after Claude already had them"
                     + (f", adding ~{fmt_tok(dup_tok)} tokens back into the conversation" if dup_tok else ""),
            monthly_usd=dup_cost,
            why="Every time a file is read, a full copy of it is added to the "
                "conversation. The first copy does not go away - so now you are "
                "paying for both, on every message that follows.",
            fix="Mostly this is Claude's habit, not yours. If you see it "
                "re-reading a file it already has, say so: 'you already read "
                "that file, use what you have'.",
            can_fix="NONE", doc=DOCS["context"]))
    else:
        findings.append(Finding("D3", "Reading the same file twice", "PASS",
                                evidence=f"{h.dup_reads} files read more than once - not enough to matter"))

    if h.denials > max(10, total * 0.02):
        # A denied call is billed, then the retry is billed again. Cost per
        # denial approximates one average turn's input.
        per_turn = 0.0
        priced = sum(v for m, c in h.by_model.items()
                     for v in [model_cost(m, c)] if v is not None)
        if h.turns:
            per_turn = priced / h.turns
        findings.append(Finding(
            "D4", "Blocked commands, then retried", "FIX",
            evidence=f"~{h.denials} times you declined a command Claude wanted to run"
                     + (f", at roughly {money(per_turn)} of wasted spend each" if per_turn else ""),
            monthly_usd=h.denials * per_turn,
            why="When you say no to a command, you have already paid for Claude's "
                "attempt. Then you pay again for whatever it tries instead. "
                "Approving the safe commands once, up front, removes both "
                "charges - and stops the interruptions.",
            fix="Add the commands you always end up approving to "
                "'permissions.allow' in your settings, so Claude stops asking. "
                "Start with read-only ones like 'ls' or 'git status' - they "
                "cannot change anything.",
            can_fix="MANUAL", doc=DOCS["settings"]))
    else:
        findings.append(Finding("D4", "Blocked commands, then retried", "PASS",
                                evidence=f"~{h.denials} commands declined - not enough to matter"))
    return findings


# --------------------------------------------------------------------------
# report
# --------------------------------------------------------------------------

BAR = "-" * 66

# The internal fix classes mean nothing to someone who has not read the plan.
# Say who actually has to do the work instead.
FIXER_LABEL = {
    "AUTO": "we can do it for you",
    "ASSISTED": "Claude can help you",
    "MANUAL": "you, by hand",
    "NONE": "nothing to install - just a habit",
}
# Plain-language status words for the summary table.
STATUS_LABEL = {
    "PASS": "all good",
    "FIX": "worth fixing",
    "N/A": "does not apply",
    "UNKNOWN": "could not tell",
}


def parse_since(since_iso, window_days):
    """Always returns a timezone-aware datetime; mixing naive and aware raises."""
    parsed = _parse_ts(since_iso) if since_iso else None
    if parsed is None:
        return dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=window_days)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=dt.timezone.utc)
    return parsed


def load_calibration_config():
    """Remembered --actual-spend, so calibration is not lost between runs.

    Without this the default run reports an uncalibrated list-price number even
    though the user already supplied a real one - which is how a ~2x-inflated
    figure ends up on screen again."""
    data = read_json(CONFIG_PATH)
    if not isinstance(data, dict):
        return None
    cal = data.get("calibration")
    if not isinstance(cal, dict) or not cal.get("actual_spend"):
        return None
    saved = _parse_ts(cal.get("saved_at"))
    if saved is not None:
        if saved.tzinfo is None:
            saved = saved.replace(tzinfo=dt.timezone.utc)
        age = (dt.datetime.now(dt.timezone.utc) - saved).days
        cal["age_days"] = age
        cal["stale"] = age > CALIBRATION_STALE_DAYS
    return cal


def save_calibration_config(actual_spend, since_iso, asof_iso):
    data = read_json(CONFIG_PATH) or {}
    data["calibration"] = {
        "actual_spend": actual_spend,
        "spend_since": since_iso,
        "spend_asof": asof_iso,
        "saved_at": dt.datetime.now(dt.timezone.utc).isoformat(),
    }
    _save_json(CONFIG_PATH, data)
    try:
        os.chmod(CONFIG_PATH, 0o600)
    except OSError:
        pass


def parse_asof(value):
    """Upper bound for the calibration period, timezone-aware."""
    if not value:
        return dt.datetime.now(dt.timezone.utc)
    parsed = _parse_ts(value)
    if parsed is None:
        return dt.datetime.now(dt.timezone.utc)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=dt.timezone.utc)
    return parsed


def calibrate(findings, h, actual_spend, cal_since=None, cal_until=None):
    """Scale dollar figures so the billing-period total matches a real amount.

    A uniform rate error scales every figure equally, so calibrating the total
    also corrects each finding - and the ranking was already unaffected."""
    if not actual_spend or actual_spend <= 0:
        return None
    est = sum(v for m, c in h.cal_by_model.items()
              for v in [model_cost(m, c)] if v is not None)
    if est <= 0:
        return None
    factor = actual_spend / est
    for f in findings:
        f.monthly_usd *= factor
    return {"factor": round(factor, 3), "list_price_estimate": round(est, 2),
            "actual_spend": actual_spend,
            "period_start": cal_since.date().isoformat() if cal_since else None,
            "period_end": cal_until.isoformat(timespec="minutes") if cal_until else None,
            "calibration_turns": sum(c["turns"] for c in h.cal_by_model.values())}


def render(findings, h, version, window_days, warnings, cal=None):
    lines = []
    total = sum(v for m, c in h.by_model.items()
                for v in [model_cost(m, c)] if v is not None)
    overhead = sum(f.tokens_per_turn for f in findings if f.status == "FIX")
    reads = sum(c["cache_read"] for c in h.by_model.values())
    writes = sum(c["w5m"] + c["w1h"] + c["w_unknown"] for c in h.by_model.values())
    fresh = sum(c["in"] for c in h.by_model.values())
    readable = reads + writes + fresh
    hit = (reads / readable * 100) if readable else 0
    exp = sum(c["turns"] for m, c in h.by_model.items()
              if any(t in m for t in EXPENSIVE_MODELS))
    share = (exp / h.turns * 100) if h.turns else 0

    if cal:
        total *= cal["factor"]
    fixes = [f for f in findings if f.status == "FIX"]
    passes = [f for f in findings if f.status == "PASS"]
    identified_t = sum(f.monthly_usd for f in fixes)
    score_t = round(len(passes) / max(len(findings), 1) * 100)
    lines.append(f"COST INSPECTOR - claude-code {version or 'unknown'} - "
                 f"{len(h.sessions)} sessions - last {window_days} days")
    lines.append(BAR)
    if identified_t:
        lines.append(f"  YOU COULD SAVE   {money(identified_t)}/mo"
                     f"{'':<6}SCORE  {len(passes)}/{len(findings)} ({score_t}%)")
    else:
        lines.append(f"  NOTHING TO FIX HERE{'':<12}SCORE  "
                     f"{len(passes)}/{len(findings)} ({score_t}%)")
    lines.append(BAR)
    label = "Calibrated spend    " if cal else "Estimated spend     "
    lines.append(f"  {label}  {money(total):>10}      "
                 f"Extra per msg  ~{fmt_tok(overhead)} tok")
    lines.append(f"  On expensive model  {share:>11.0f}%      "
                 f"Text re-used  {hit:.1f}%")
    if cal:
        period = ""
        if cal.get("period_start"):
            period = f" for {cal['period_start']}..{(cal.get('period_end') or '')[:10]}"
        lines.append(f"  (calibrated x{cal['factor']} against "
                     f"{money(cal['actual_spend'])} billed{period})")
    else:
        lines.append("  ** These are public list prices, usually ~1.8x higher than a "
                     "real bill **")
        lines.append("  Tell me what you were actually billed and I will correct "
                     "every figure, and remember it.")
    lines.append("")
    lines.append(f"  {len(passes)} of {len(findings)} checks passed"
                 f"{'':<20}{len(fixes)} things worth fixing")
    lines.append(BAR)

    for w in warnings:
        lines.append(f"  ! {w}")
    if warnings:
        lines.append(BAR)

    for f in sorted(fixes, key=lambda x: -x.monthly_usd):
        impact = []
        if f.tokens_per_turn:
            impact.append(f"~{fmt_tok(f.tokens_per_turn)} tokens per message")
        if f.monthly_usd:
            impact.append(f"~{money(f.monthly_usd)}/mo")
        tag = "  ".join(impact) or "cost not quantified"
        lines.append("")
        lines.append(f"FIX  {f.title}{'':<4}{tag}")
        lines.append(f"     Who fixes it: {FIXER_LABEL.get(f.can_fix, f.can_fix)}")
        lines.append(f"     Found: {f.evidence}")
        lines.append("")
        for i, chunk in enumerate(_wrap(f.why, 57)):
            lines.append(f"     Why this costs: {chunk}" if i == 0
                         else f"                     {chunk}")
        lines.append("")
        for i, chunk in enumerate(_wrap(f.fix, 57)):
            lines.append(f"     What to do:     {chunk}" if i == 0
                         else f"                     {chunk}")
        if f.doc:
            lines.append(f"     Docs: {f.doc}")

    lines.append("")
    lines.append(BAR)
    for f in findings:
        if f.status == "FIX":
            continue
        lines.append(f"{STATUS_LABEL.get(f.status, f.status):<16}{f.title} - {f.evidence}")
    return "\n".join(lines)


SELF_PATH = os.path.abspath(__file__)
HOOK_PATH = os.path.join(os.path.dirname(SELF_PATH), "first_prompt_nudge.py")


def open_in_viewer(path):
    """Open the saved report in whatever the OS uses for .md files.

    A file path in a chat reply is a dead end: the user is told where the
    report is rather than being shown it. This is the cheap fix - it costs no
    tokens and nothing leaves the machine, unlike pasting 11k characters into
    the conversation or publishing it somewhere.

    The path is one we just built ourselves, never user input, and the command
    is passed as a list so there is no shell to interpret it."""
    opener = {"darwin": "open", "win32": "start"}.get(sys.platform, "xdg-open")
    try:
        subprocess.run([opener, path], check=True,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                       timeout=10)
        print("  Opened it in your default markdown viewer.")
    except (OSError, subprocess.SubprocessError):
        # Headless box, no desktop, no handler for .md - all fine, the file is
        # already written and its path is already printed.
        print("  (could not open it automatically - the path above still works)")


def _apply_cmd(ids):
    """Absolute AND quoted: the saved report is read from anywhere, a relative
    `scripts/audit.py` only resolves inside the skill dir, and an unquoted path
    breaks on the first space in a directory name."""
    return f'python3 "{SELF_PATH}" --apply {ids}'


def render_markdown(findings, h, version, window_days, warnings, cal, ref):
    """Markdown twin of render(). The terminal wants fixed-width columns; a
    saved .md file wants real markdown, not ASCII art with a renamed suffix."""
    total = sum(v for m, c in h.by_model.items()
                for v in [model_cost(m, c)] if v is not None)
    if cal:
        total *= cal["factor"]
    overhead = sum(f.tokens_per_turn for f in findings if f.status == "FIX")
    reads = sum(c["cache_read"] for c in h.by_model.values())
    writes = sum(c["w5m"] + c["w1h"] + c["w_unknown"] for c in h.by_model.values())
    fresh = sum(c["in"] for c in h.by_model.values())
    readable = reads + writes + fresh
    hit = (reads / readable * 100) if readable else 0
    exp = sum(c["turns"] for m, c in h.by_model.items()
              if any(t in m for t in EXPENSIVE_MODELS))
    share = (exp / h.turns * 100) if h.turns else 0
    fixes = [f for f in findings if f.status == "FIX"]
    passes = [f for f in findings if f.status == "PASS"]

    fixes_sorted = sorted(fixes, key=lambda x: -x.monthly_usd)
    identified = sum(f.monthly_usd for f in fixes)
    auto_save = sum(f.monthly_usd for f in fixes if f.can_fix == "AUTO")
    score = round(len(passes) / max(len(findings), 1) * 100)
    spend_label = "Calibrated spend" if cal else "Estimated spend"

    out = ["# Cost Inspector report", ""]

    # Bottom line first, then short sections. An earlier version put ~7.7k
    # characters of reasoning on screen at once and read as a wall of text,
    # which is the one way a report like this fails: nobody acts on it.
    #
    # Collapsing it behind <details> was tried and reverted: the Claude Code
    # desktop app's markdown renderer does not process inline HTML, so readers
    # there saw literal "<details>" tags. Anything relying on an HTML-capable
    # viewer is off the table - brevity has to come from writing less.
    if identified:
        pct = (identified / total * 100) if total else 0
        out.append(f"## You could save about {money(identified)} a month")
        out.append("")
        out.append(f"That is {pct:.0f}% of your {money(total)} "
                   f"{spend_label.lower()}. **{len(passes)} of {len(findings)} "
                   f"checks passed.**")
    else:
        out.append("## Nothing worth changing")
        out.append("")
        out.append(f"**{len(passes)} of {len(findings)} checks passed**, and the "
                   "rest have no measurable cost.")
    out.append("")

    if fixes_sorted:
        top = fixes_sorted[0]
        out.append(f"**Start here:** {top.title} — {money(top.monthly_usd)} a "
                   f"month, the biggest single win.")
        out.append("")
        out.append("| What we found | Saves/month | Who does it |")
        out.append("|---|---|---|")
        for f in fixes_sorted:
            out.append(f"| {f.title} | {money(f.monthly_usd)} "
                       f"| {FIXER_LABEL.get(f.can_fix, f.can_fix)} |")
        out.append("")

    for w in warnings:
        out.append(f"> [!NOTE]")
        out.append(f"> {w}")
        out.append("")

    if fixes:
        auto = [f.id for f in fixes if f.can_fix == "AUTO"]
        out.append("## What to do")
        out.append("")
        if auto:
            out.append(f"{money(auto_save)}/month of this is automatic. Nothing "
                       "changes unless you run it, and a dated backup is saved "
                       "first:")
            out.append("")
            out.append("```bash")
            out.append(_apply_cmd(",".join(auto)))
            out.append("```")
            out.append("")

        for f in sorted(fixes, key=lambda x: -x.monthly_usd):
            bits = []
            if f.monthly_usd:
                bits.append(f"{money(f.monthly_usd)}/mo")
            if f.tokens_per_turn:
                bits.append(f"~{fmt_tok(f.tokens_per_turn)} tokens per message")
            impact = " · ".join(bits) or "not quantified"
            # Heading carries impact and owner, so the body does not repeat them.
            out.append(f"### {f.title}")
            out.append("")
            out.append(f"`{impact}` · "
                       f"{FIXER_LABEL.get(f.can_fix, f.can_fix)}")
            out.append("")
            out.append(f"{f.evidence}")
            out.append("")
            out.append(f.why)
            out.append("")
            out.append(f"**Do this:** {f.fix}")
            out.append("")
            if f.can_fix == "AUTO":
                out.append("```bash")
                out.append(_apply_cmd(f.id))
                out.append("```")
                out.append("")
            if f.doc:
                out.append(f"[More on this]({f.doc})")
                out.append("")

    out.append("## Everything else")
    out.append("")

    out.append(f"### {len(passes)} checks passed — nothing to do")
    out.append("")
    out.append("| | Check | What we saw |")
    out.append("|---|---|---|")
    for f in findings:
        if f.status == "FIX":
            continue
        out.append(f"| {STATUS_LABEL.get(f.status, f.status)} | {f.title} "
                   f"| {f.evidence} |")
    out.append("")

    out.append(f"### Your numbers — {len(h.sessions)} sessions over "
               f"{window_days} days")
    out.append("")
    out.append("| | | |")
    out.append("|---|---|---|")
    out.append(f"| **{spend_label}** | {money(total)} | what those sessions "
               "cost |")
    out.append(f"| Always-on extra | ~{fmt_tok(overhead)} tokens per message | "
               "instructions, skill names and memory, re-sent every time you "
               "hit enter |")
    out.append(f"| On an expensive model | {share:.0f}% of messages | Opus or "
               "Fable rather than Sonnet or Haiku |")
    out.append(f"| Re-used text | {hit:.1f}% | served from cache at a tenth of "
               "the price. Higher is better |")
    out.append("")
    if cal:
        period = ""
        if cal.get("period_start"):
            period = (f" over {cal['period_start']} to "
                      f"{(cal.get('period_end') or '')[:16]}")
        out.append(f"You told us you were actually billed "
                   f"{money(cal['actual_spend'])}{period}. Our own maths said "
                   f"{money(cal['list_price_estimate'])} for the same period, so "
                   f"every figure here is scaled by **x{cal['factor']}** to match "
                   f"your real bill.")
    else:
        out.append("These come from public list prices, so they usually land "
                   "**higher than a real bill** — about 1.8x higher on the one "
                   "account we checked. Re-run with `--actual-spend <amount> "
                   "--spend-since <date>` and every figure gets corrected, and "
                   "remembered. The *order* of the findings is right either way.")
    out.append("")
    out.append(f"Run {dt.datetime.now().strftime('%d %b %Y at %H:%M')} on Claude "
               f"Code {version or 'unknown'}.")
    out.append("")

    out.append("### Want a deeper look at how you work?")
    out.append("")
    if ref.get("recommended"):
        out.append("Everything above came from settings and totals. It cannot "
                   "see *how* you work — whether questions start clearly, "
                   "whether work gets redone. The companion **cost-coach** skill "
                   "reads a few of your actual conversations and tells you that "
                   f"part. Your results suggest it is worth a look "
                   f"({ref['reason']}). It would read your 5 most expensive "
                   f"sessions for about **${ref['forecast_usd']:.2f}**, because "
                   "reading conversations is not free.")
    else:
        out.append("The companion **cost-coach** skill reads a few of your "
                   "actual conversations and comments on how you work. Your "
                   "sessions already look efficient, so it probably would not "
                   "tell you enough to be worth what it costs.")
    out.append("")
    out.append("---")
    out.append("")
    out.append("*Built only from your settings files and counters like token "
               "totals and tool names. It never opened one of your "
               "conversations.*")
    return "\n".join(out)


def _wrap(text, width):
    words, line, out = text.split(), "", []
    for w in words:
        if len(line) + len(w) + 1 > width:
            out.append(line)
            line = w
        else:
            line = f"{line} {w}".strip()
    if line:
        out.append(line)
    return out


def referral(findings, h):
    """One line, evidence-based, and willing to say don't."""
    signals = [f.id for f in findings
               if f.id in ("D1", "D2", "D3") and f.status == "FIX"]
    peak = _forecast_coach_cost(h)
    if not signals:
        return {"recommended": False,
                "reason": "session patterns look efficient",
                "signals": [], "forecast_usd": peak}
    names = {"D1": "low subagent use", "D2": "heavy screenshot use",
             "D3": "repeated file reads"}
    return {"recommended": True,
            "reason": "; ".join(names[s] for s in signals),
            "signals": signals, "forecast_usd": peak}


def _forecast_coach_cost(h, n=5):
    """Cost of reading the n largest sessions' text at Sonnet input rates.
    A session's peak context is a reasonable proxy for its content size."""
    peaks = sorted(h.session_peak.values(), reverse=True)[:n]
    return round(sum(peaks) / 1e6 * PRICES["claude-sonnet-5"][0], 2)


def write_findings(findings, h, version, window_days, ref, cal=None):
    payload = {
        "schema_version": SCHEMA_VERSION,
        "written_at": dt.datetime.now(dt.timezone.utc).isoformat(),
        "claude_version": version,
        "audit_window_days": window_days,
        "calibration": cal,
        "referral": ref,
        "findings": [f.as_json() for f in findings],
    }
    os.makedirs(os.path.dirname(FINDINGS_PATH), exist_ok=True)
    tmp = FINDINGS_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, indent=2)
    os.replace(tmp, FINDINGS_PATH)
    try:
        os.chmod(FINDINGS_PATH, 0o600)
    except OSError:
        pass
    return FINDINGS_PATH


# --------------------------------------------------------------------------
# fixes
# --------------------------------------------------------------------------

def hook_installed(settings_path):
    data = read_json(settings_path) or {}
    for entry in (data.get("hooks") or {}).get("UserPromptSubmit") or []:
        for hook in (entry or {}).get("hooks") or []:
            if "first_prompt_nudge" in str(hook.get("command", "")):
                return True
    return False


def install_hook(settings_path):
    """Add the first-prompt nudge as a UserPromptSubmit hook.

    This is the only fix for B1 the tool can actually apply: model choice is a
    per-session decision, so a one-off settings change cannot right-size it.
    The hook asks Claude to judge each session's first prompt instead."""
    if not os.path.isfile(HOOK_PATH):
        return ["hook script missing - reinstall the skill"]
    data = read_json(settings_path) or {}
    if hook_installed(settings_path):
        return ["first-prompt nudge already installed"]
    if os.path.exists(settings_path):
        backup(settings_path)
    hooks = data.setdefault("hooks", {})
    entries = hooks.setdefault("UserPromptSubmit", [])
    entries.append({"hooks": [{"type": "command", "timeout": 10,
                               "command": f"python3 {HOOK_PATH}"}]})
    _save_json(settings_path, data)
    return [f"installed first-prompt nudge hook -> {HOOK_PATH}",
            "it fires once per session and stays silent unless the task is "
            "over-provisioned"]


def remove_hook(settings_path):
    data = read_json(settings_path) or {}
    hooks = (data.get("hooks") or {})
    entries = hooks.get("UserPromptSubmit") or []
    kept = []
    for entry in entries:
        inner = [h for h in (entry or {}).get("hooks") or []
                 if "first_prompt_nudge" not in str(h.get("command", ""))]
        if inner:
            kept.append({**entry, "hooks": inner})
    if len(kept) == len(entries) and all(
            len(e.get("hooks") or []) == len((entries[i] or {}).get("hooks") or [])
            for i, e in enumerate(kept)):
        return ["first-prompt nudge was not installed"]
    if os.path.exists(settings_path):
        backup(settings_path)
    if kept:
        hooks["UserPromptSubmit"] = kept
    else:
        hooks.pop("UserPromptSubmit", None)
    _save_json(settings_path, data)
    return ["removed first-prompt nudge hook"]


def backup(path):
    stamp = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    dest = f"{path}.bak-{stamp}"
    shutil.copy2(path, dest)
    return dest


def apply_fix(finding, settings_path):
    """Apply one AUTO fix. Returns a list of human-readable actions."""
    done = []
    if finding.id == "A4":
        for path in finding.payload.get("paths", []):
            try:
                with open(path, encoding="utf-8") as fh:
                    text = fh.read()
            except OSError:
                continue
            fm = parse_frontmatter(text)
            if fm is None or "disable-model-invocation" in fm:
                continue
            backup(path)
            new_fm = fm + "\ndisable-model-invocation: true"
            text = text.replace(fm, new_fm, 1)
            with open(path, "w", encoding="utf-8") as fh:
                fh.write(text)
            done.append(f"disabled auto-invocation: {_short(path)}")
    elif finding.id == "A5":
        data = read_json(settings_path) or {}
        enabled = data.get("enabledPlugins")
        if not isinstance(enabled, dict):
            return ["A5 skipped: no enabledPlugins map in settings"]
        if os.path.exists(settings_path):
            backup(settings_path)
        changed = False
        for plugin in finding.payload.get("plugins", []):
            for key in list(enabled):
                if key.split("@")[0] == plugin and enabled[key] is not False:
                    enabled[key] = False
                    changed = True
                    done.append(f"disabled plugin: {key}")
        if changed:
            _save_json(settings_path, data)
    elif finding.id == "C2":
        data = read_json(settings_path) or {}
        if os.path.exists(settings_path):
            backup(settings_path)
        data["autoCompactEnabled"] = True
        _save_json(settings_path, data)
        done.append("set autoCompactEnabled=true")
    elif finding.id == "B2":
        done.append("B2 needs an env var in your shell profile: "
                    "export CLAUDE_CODE_SUBAGENT_MODEL=claude-sonnet-5")
    return done or [f"{finding.id}: nothing to change"]


def _save_json(path, data):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=2)
    os.replace(tmp, path)


# --------------------------------------------------------------------------

def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--days", type=int, default=30)
    ap.add_argument("--json", action="store_true", help="emit findings as JSON")
    ap.add_argument("--apply", metavar="IDS",
                    help="comma-separated AUTO finding ids to apply (e.g. A4,A5)")
    ap.add_argument("--actual-spend", type=float, metavar="USD",
                    help="your real billed amount, to calibrate list-price estimates")
    ap.add_argument("--spend-since", metavar="YYYY-MM-DD",
                    help="start of the billing period that --actual-spend covers")
    ap.add_argument("--spend-asof", metavar="YYYY-MM-DD[THH:MM]",
                    help="when the --actual-spend figure was read (default: now). "
                         "Usage after this moment is excluded from the "
                         "calibration, so the factor does not decay as you keep "
                         "working")
    ap.add_argument("--report", metavar="PATH",
                    help=f"where to save the readable report "
                         f"(default {_short(REPORT_PATH)})")
    ap.add_argument("--no-report", action="store_true",
                    help="print only, do not save a report file")
    ap.add_argument("--open", dest="open_report", action="store_true",
                    help="open the saved report in your default markdown "
                         "viewer once it is written")
    ap.add_argument("--calibration-status", action="store_true",
                    help="print whether a calibration is saved, and exit. Cheap "
                         "(no transcript scan) - call this first so you can ask "
                         "the user for their real spend before running")
    ap.add_argument("--install-hook", action="store_true",
                    help="install the first-prompt model/effort nudge hook")
    ap.add_argument("--remove-hook", action="store_true",
                    help="remove the first-prompt nudge hook")
    args = ap.parse_args()

    if args.calibration_status:
        saved = load_calibration_config()
        if not saved:
            print(json.dumps({
                "calibrated": False,
                "action": "ask_user",
                "prompt": "Ask the user for their actual Claude Code spend and "
                          "the date the billing period started, then re-run "
                          "with --actual-spend and --spend-since. Without it "
                          "every dollar figure is a list-price estimate, "
                          "typically ~1.8x too high.",
                "where_to_find": "their Claude usage/limits panel shows "
                                 "'$X of $Y spent' and the reset date",
            }, indent=2))
        else:
            print(json.dumps({
                "calibrated": True,
                "actual_spend": saved.get("actual_spend"),
                "spend_since": saved.get("spend_since"),
                "age_days": saved.get("age_days"),
                "stale": saved.get("stale", False),
                "action": ("ask_user_refresh" if saved.get("stale") else "proceed"),
            }, indent=2))
        return 0

    cwd = os.getcwd()
    user_settings = os.path.join(CLAUDE_DIR, "settings.json")
    if args.install_hook or args.remove_hook:
        fn = install_hook if args.install_hook else remove_hook
        for line in fn(user_settings):
            print(f"  {line}")
        return 0
    version = claude_version()
    warnings = []
    if not version:
        warnings.append("could not work out which Claude Code version you have, so some checks may be out of date")
    elif not version_tested(version):
        warnings.append(f"your Claude Code ({version}) is newer than the versions this "
                        f"tool was tested against ({', '.join(TESTED_VERSIONS)}*). Session "
                        "history may have changed shape, so anything we cannot read "
                        "is reported as 'could not tell' rather than guessed")

    settings, sources = load_settings(cwd)
    skills = scan_skills()
    md = scan_claude_md(cwd)
    servers = mcp_servers()
    # Calibration is remembered: without this, the next plain run reports an
    # uncalibrated list-price figure even though the user already gave a real
    # one - which is how a ~2x-inflated number lands back on screen.
    actual_spend = args.actual_spend
    spend_since, spend_asof = args.spend_since, args.spend_asof
    remembered = None
    if actual_spend:
        save_calibration_config(actual_spend, spend_since, spend_asof)
    else:
        remembered = load_calibration_config()
        if remembered:
            actual_spend = remembered.get("actual_spend")
            spend_since = spend_since or remembered.get("spend_since")
            spend_asof = spend_asof or remembered.get("spend_asof")
            if remembered.get("stale"):
                warnings.append(
                    f"the billed amount you gave us is {remembered.get('age_days')} days "
                    "old, so the dollar figures are drifting. Check your "
                    "usage page and re-run with a fresh --actual-spend")
            # No warning in the healthy case: both outputs already carry a
            # "these figures were scaled to your real bill" line, and repeating
            # it as a NOTE just makes a clean run look like it has problems.

    cal_since = parse_since(spend_since, args.days) \
        if actual_spend else None
    cal_until = parse_asof(spend_asof) if actual_spend else None
    h = scan_history(args.days, cal_since, cal_until)

    if h.bad_lines:
        warnings.append(f"{h.bad_lines} lines of your session history were unreadable and "
                        "skipped, so the totals are slightly low")
    if h.missing_usage:
        warnings.append(f"{h.missing_usage} replies did not record their token counts, so "
                        "the totals are low by that much")
    if h.subagent_turns:
        warnings.append("records of helper subagents are kept in a temporary "
                        "folder that your computer clears out, so older helper "
                        "costs are only partly visible here")

    wmult = dominant_write_mult(h)
    findings = []
    elsewhere = other_project_claude_md(cwd, h.cwds)
    findings += check_claude_md(md, h, wmult, elsewhere)
    findings += check_skills(skills, h, args.days, wmult,
                             settings.get("enabledPlugins"))
    findings += check_memory(cwd, h, wmult)
    findings += check_mcp(servers, h, args.days, wmult)
    findings += check_models(h, settings, sources)
    findings += check_cache(h, settings)
    findings += check_habits(h)

    cal = calibrate(findings, h, actual_spend, cal_since, cal_until)
    ref = referral(findings, h)
    path = write_findings(findings, h, version, args.days, ref, cal)

    if args.apply:
        wanted = {x.strip().upper() for x in args.apply.split(",") if x.strip()}
        target = os.path.join(CLAUDE_DIR, "settings.json")
        for f in findings:
            if f.id in wanted and f.status == "FIX" and f.can_fix == "AUTO":
                for line in apply_fix(f, target):
                    print(f"  {line}")
            elif f.id in wanted:
                print(f"  {f.id}: not an applicable AUTO fix (status={f.status}, "
                      f"can_fix={f.can_fix})")
        return 0

    if args.json:
        print(json.dumps({"findings": [f.as_json() for f in findings],
                          "referral": ref, "warnings": warnings}, indent=2))
        return 0

    report = render(findings, h, version, args.days, warnings, cal)
    tail = []
    if ref["recommended"]:
        tail.append(f"  Worth a deeper look ({ref['reason']}). The companion "
                    f"cost-coach skill reads your actual conversations and "
                    f"comments on how you work - it would read your 5 most "
                    f"expensive sessions for about ${ref['forecast_usd']:.2f}.")
    else:
        tail.append("  Your sessions already look efficient, so the companion "
                    "cost-coach skill (which reads your conversations) probably "
                    "would not tell you enough to be worth its cost.")
    report = report + "\n\n" + "\n".join(tail)

    print(report)
    print(f"  Findings written to {os.path.abspath(path)}")
    if not args.no_report:
        dest = args.report or REPORT_PATH
        md = render_markdown(findings, h, version, args.days, warnings, cal, ref)
        try:
            os.makedirs(os.path.dirname(os.path.abspath(dest)), exist_ok=True)
            with open(dest, "w", encoding="utf-8") as fh:
                fh.write(md + "\n")
            print(f"  Report saved to {os.path.abspath(dest)}")
            if args.open_report:
                open_in_viewer(os.path.abspath(dest))
        except OSError:
            print("  (could not save the report file)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
