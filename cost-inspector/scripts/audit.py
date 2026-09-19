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
# Fast mode runs the same model faster at premium rates. Documented for
# Opus 5 only ($10/$50); Opus 4.8 also offers it but its fast price is not
# published, so it stays at standard rather than being guessed at.
FAST_PRICES = {
    "claude-opus-5": (10.0, 50.0),
}
FAST_SUFFIX = "#fast"

# Every threshold in one place, each labelled with where it came from.
# PLAN.md principle #1 forbids numbers baked in from the author's machine.
# Three kinds appear here, and the distinction is the point:
#   DOC       - published by Anthropic; safe to hard-code
#   DERIVED   - computed from the machine being audited
#   ASSUMED   - our own judgement. These are the ones to be suspicious of,
#               and every one of them is disclosed in the finding text.
THRESHOLDS = {
    "claude_md_lines": (200, "DOC", "docs: target under 200 lines"),
    "skill_desc_chars": (1536, "DOC", "docs: description truncated at 1,536"),
    "memory_lines": (200, "DOC", "docs: first 200 lines of MEMORY.md load"),
    "memory_bytes": (25 * 1024, "DOC", "docs: or the first 25KB"),
    "import_depth": (4, "DOC", "docs: maximum of four hops"),
    "long_session_vs_median": (4, "DERIVED", "multiple of THIS machine's median session"),
    "long_session_floor": (100_000, "ASSUMED", "below this a session is not 'long' on a 1M context"),
    "cache_hit_ok": (0.8, "ASSUMED", "no published guidance on a healthy hit rate"),
    "expensive_share": (0.4, "ASSUMED", "share of messages before the model is worth raising"),
    "screenshot_floor": (20, "ASSUMED", "below this the total is rounding error"),
    "dup_read_floor": (20, "ASSUMED", "as above"),
    "image_tokens": (2600, "ASSUMED", "width x height / 750 for a retina window capture"),
    "effort_cut_per_step": (0.15, "ASSUMED", "reduction per step down the effort scale"),
    "project_file_lines": (150, "ASSUMED", "before splitting a project file is worth it"),
    "active_subdirs": (3, "ASSUMED", "sub-folders worked in before splitting pays"),
}


def threshold(name):
    return THRESHOLDS[name][0]


CACHE_READ_MULT = 0.1
# Fable reads cache at $0.25/MTok against a $10/MTok input rate - 0.025x, not
# the 0.1x every other model gets. Applying the flat rate overstated Fable
# cache cost fourfold.
CACHE_READ_MULT_BY_MODEL = {
    "claude-fable-5-1": 0.025,
    "claude-fable-5": 0.025,
}
WRITE_MULT_5M = 1.25
WRITE_MULT_1H = 2.0
CHARS_PER_TOKEN = 4  # coarse; only used for on-disk text we cannot tokenize

# Where a conversation starts being expensive to carry, in absolute tokens.
# Deliberately not a fraction of the context window: a message carrying 500k
# costs five times one carrying 100k, and the window limit does not change
# that. The window is not recoverable from a transcript anyway - it records
# `claude-opus-5` whether or not the session ran with the 1M setting.
HIGH_CONTEXT_TOKENS = 100_000
HIGH_CONTEXT_MIN_RUN = 25   # below this it is one long task, not a habit

# List-price estimates do not match real bills. Measured against one Enterprise
# account, this model ran 1.82x the actual billed amount - negotiated rates,
# plan discounts and billing details we cannot see all push the same way.
# So: dollars are INDICATIVE. What survives the error is the *ranking* of
# findings and their ratios to each other, because a uniform rate error scales
# every figure equally. Pass --actual-spend to calibrate against a real number.
CALIBRATION_NOTE = ("list-price estimate, typically higher than a real bill; "
                    "use --actual-spend to calibrate")

EXPENSIVE_MODELS = ("opus", "fable")


def _base_model(model):
    """Strip the fast-mode marker and any date suffix."""
    base = (model or "").replace(FAST_SUFFIX, "")
    return re.sub(r"-\d{8}$", "", base)


def price_for(model):
    """Per-MTok (input, output). Fast-mode turns are tagged with FAST_SUFFIX
    during parsing and priced from FAST_PRICES."""
    if (model or "").endswith(FAST_SUFFIX):
        base = _base_model(model)
        if base in FAST_PRICES:
            return FAST_PRICES[base]
        return PRICES.get(base)  # premium unpublished - do not invent one
    if model in PRICES:
        return PRICES[model]
    return PRICES.get(_base_model(model))


def cache_read_mult(model):
    return CACHE_READ_MULT_BY_MODEL.get(_base_model(model), CACHE_READ_MULT)


def blended_cache_read_rate(h):
    """Weighted $/MTok actually paid for a cache read, across the models in
    use. Replaces `blended_input_rate(h) * CACHE_READ_MULT`, which assumed
    every model discounts cache reads by the same factor."""
    num = den = 0.0
    for model, c in h.by_model.items():
        pr = price_for(model)
        if not pr:
            continue
        num += pr[0] * cache_read_mult(model) * c["turns"]
        den += c["turns"]
    return (num / den) if den else 0.5


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

    # "measured"  - always-on overhead you pay whatever you do, removed by a
    #               config change. Plain arithmetic, and the checks do not
    #               overlap each other, so these can safely be summed.
    # "bound"     - depends on a behavioural counterfactual (a cheaper model
    #               could have done it, you would have cleared the session).
    #               These overlap each other heavily - B1 reprices the very
    #               cache reads C2 calls avoidable - so they are ranked and
    #               shown, never added together or added to the measured
    #               total.
    def __init__(self, cid, title, status, evidence="", why="", fix="",
                 can_fix="NONE", doc="", tokens_per_turn=0, monthly_usd=0.0,
                 payload=None, kind="measured"):
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
        self.kind = kind

    def as_json(self):
        return {
            "id": self.id,
            "title": self.title,
            "status": self.status,
            "can_fix": self.can_fix,
            "tokens_per_turn": round(self.tokens_per_turn),
            "monthly_usd": round(self.monthly_usd, 2),
            "kind": self.kind,
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


def mcp_servers(cwd=None):
    """MCP servers split by whether they actually load in THIS session.

    `~/.claude.json` holds a per-project map. Walking the whole file collects
    every server the user has ever configured anywhere and bills all of them
    as always-on overhead here, which is wrong: project-scoped servers are
    matched on the exact directory and do not inherit into subfolders.

    Returns {"active": set, "other_projects": {name: [dirs]}, "needs_auth": set}.
    Only "active" may be priced.
    """
    cwd = os.path.realpath(cwd or os.getcwd())
    active, other = set(), collections.defaultdict(list)

    root = read_json(os.path.join(HOME, ".claude.json")) or {}
    # User scope: top-level mcpServers, loaded in every session.
    if isinstance(root.get("mcpServers"), dict):
        active.update(root["mcpServers"].keys())

    # Project scope: exact-directory match only.
    projects = root.get("projects")
    if isinstance(projects, dict):
        for path, cfg in projects.items():
            if not isinstance(cfg, dict):
                continue
            names = set(cfg.get("mcpServers", {}) or {})
            names.update(cfg.get("enabledMcpjsonServers", []) or [])
            if not names:
                continue
            try:
                same = os.path.realpath(path) == cwd
            except OSError:
                same = False
            if same:
                active.update(names)
            else:
                for n in names:
                    other[n].append(path)

    # settings.json is user scope; .mcp.json is this directory.
    for path in (os.path.join(CLAUDE_DIR, "settings.json"),
                 os.path.join(cwd, ".mcp.json")):
        data = read_json(path)
        if isinstance(data, dict) and isinstance(data.get("mcpServers"), dict):
            active.update(data["mcpServers"].keys())

    # A .mcp.json server the user explicitly declined does not load.
    here = (projects or {}).get(cwd) if isinstance(projects, dict) else None
    if isinstance(here, dict):
        active -= set(here.get("disabledMcpjsonServers", []) or [])

    # Servers still waiting on an OAuth flow register no tools until approved.
    auth = read_json(os.path.join(CLAUDE_DIR, "mcp-needs-auth-cache.json"))
    needs_auth = set()
    if isinstance(auth, dict):
        for key in auth:
            short = str(key).split(":")[-1].strip()
            for name in list(active):
                if name == key or name.lower() == short.lower():
                    needs_auth.add(name)

    other = {n: dirs for n, dirs in other.items() if n not in active}
    return {"active": active, "other_projects": other, "needs_auth": needs_auth}


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


def scan_skills(cwd=None):
    """Inventory SKILL.md files. Personal ones are writable; plugin ones are
    not. Project skills live in the repo and were previously invisible."""
    out = []
    roots = [(os.path.join(CLAUDE_DIR, "skills"), "personal"),
             (os.path.join(CLAUDE_DIR, "plugins"), "plugin")]
    if cwd:
        roots.append((os.path.join(os.path.realpath(cwd), ".claude", "skills"),
                      "personal"))
    for root, kind in roots:
        if not os.path.isdir(root):
            continue
        for path in glob.glob(os.path.join(root, "**", "SKILL.md"), recursive=True):
            try:
                with open(path, encoding="utf-8", errors="replace") as fh:
                    text = fh.read()
                st = os.stat(path)
                # Creation time where the platform has it: mtime resets when
                # a year-old skill is edited, which exempted it from the age
                # gate as though it were brand new.
                mtime = getattr(st, "st_birthtime", None) or st.st_mtime
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
                # The skill listing truncates description text at 1,536
                # characters, so a longer one costs no more than that.
                "bytes": len(name) + min(len(desc), threshold("skill_desc_chars")), "disabled": disabled,
                "mtime": mtime,
            })
    return out


# Imports need not sit alone on a line. Code spans and fenced blocks are
# skipped, per the docs, so `@README` in backticks stays literal.
IMPORT_RE = re.compile(r"(?<![\w`])@([^\s`]+)")
FENCE_RE = re.compile(r"```.*?```", re.S)
SPAN_RE = re.compile(r"`[^`\n]*`")
MAX_IMPORT_DEPTH = 4  # the platform expands up to four hops


def find_imports(text, base_dir, _depth=1, _seen=None):
    """Resolve @imports, recursively.

    Only counts targets that exist on disk - a bare regex also matches
    decorators (@Injectable), npm scopes (@angular/core) and email
    addresses. Imported files can import others up to four hops, and the
    previous single-level scan undercounted every nested chain.
    """
    if _depth > MAX_IMPORT_DEPTH:
        return []
    _seen = _seen if _seen is not None else set()
    stripped = SPAN_RE.sub("", FENCE_RE.sub("", text))
    found = []
    for raw in IMPORT_RE.findall(stripped):
        target = os.path.expanduser(raw)
        if not os.path.isabs(target):
            target = os.path.join(base_dir, target)
        real = os.path.realpath(target)
        if real in _seen or not os.path.isfile(target):
            continue
        _seen.add(real)
        try:
            size = os.path.getsize(target)
            with open(target, encoding="utf-8", errors="replace") as fh:
                nested = fh.read()
        except OSError:
            size, nested = 0, ""
        found.append({"spec": raw, "path": target, "bytes": size,
                      "depth": _depth})
        found.extend(find_imports(nested, os.path.dirname(target),
                                  _depth + 1, _seen))
    return found


def claude_md_excludes(settings):
    """Glob patterns that stop a CLAUDE.md loading at all.

    A monorepo lever: other teams' instruction files would otherwise stack up
    on every message. A file matched here costs nothing, so counting it would
    overstate; and if the user has none, saying so is a real suggestion."""
    pats = settings.get("claudeMdExcludes") if isinstance(settings, dict) else None
    return [str(x) for x in pats] if isinstance(pats, list) else []


def _excluded(path, patterns):
    if not patterns:
        return False
    import fnmatch
    p = os.path.realpath(path)
    return any(fnmatch.fnmatch(p, pat) or fnmatch.fnmatch(os.path.basename(p), pat)
               or pat in p for pat in patterns)


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
        # A plugin ships agents and MCP servers too, not just skills. Counting
        # only Skill calls let A5 mark a plugin unused - and AUTO-disable it -
        # while its agents were in daily use.
        self.agent_invocations = collections.Counter()
        self.sidechain_turns = 0
        self.subagent_turns = 0
        self.subagent_models = collections.Counter()
        self.mcp_used = set()
        # server -> set(tool names), across ALL history. The windowed
        # tool_calls counter undercounts tools-per-server badly, and that
        # average is what A6 extrapolates from.
        self.mcp_tools_seen = collections.defaultdict(set)
        self.denials = 0
        self.dup_reads = 0
        self.image_reads = 0
        self.fast_turns = 0
        self.dup_read_bytes = 0
        self.earliest = None
        self.latest = None
        self.bad_lines = 0        # malformed JSON in a .jsonl transcript
        self.text_lines = 0       # expected plain text in mixed-format .output
        self.session_peak = {}    # session key -> peak context tokens
        # session key -> cache-read tokens. Peak context is a snapshot; this
        # is the volume actually re-sent, which is what carrying a long
        # conversation costs.
        self.session_cache_read = collections.Counter()
        self.session_writes = collections.Counter()
        # Peak alone says how big a session got, not how long it stayed big -
        # and the bill is the second one. A session that touched 400k on its
        # last message costs far less than one that sat at 300k for 400
        # messages, yet peak ranks them the same way.
        self.session_high_turns = {}   # key -> messages above HIGH_CONTEXT
        self.session_high_run = {}     # key -> longest unbroken such stretch
        self.session_cur_run = {}      # key -> current stretch (working state)
        self.session_carried = {}      # key -> tokens carried above the line
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
                # Claude Code records which speed served the turn. Fast mode
                # is the same model at roughly double the rate, so it has to
                # be priced separately or those turns undercount by 2x.
                if isinstance(usage, dict) and usage.get("speed") == "fast":
                    model += FAST_SUFFIX
                    h.fast_turns += 1
                if isinstance(usage, dict):
                    # Calibration is bounded by cal_since/cal_until, NOT by the
                    # reporting window. Nesting it inside the window gate made
                    # the correction factor depend on --days: on this machine
                    # --days 7 gave x1.581 and --days 30 gave x0.551 for the
                    # same billing period, a 2.9x swing in every dollar figure
                    # from a flag that should only change what is reported.
                    if (cal_since is not None and ts is not None
                            and ts >= cal_since
                            and (cal_until is None or ts <= cal_until)):
                        _accumulate(h.cal_by_model[model], usage)
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
                        h.sessions.add(key)
                        ctx = ((usage.get("input_tokens") or 0)
                               + (usage.get("cache_read_input_tokens") or 0))
                        if ctx > h.session_peak.get(key, 0):
                            h.session_peak[key] = ctx
                        h.session_cache_read[key] += (
                            usage.get("cache_read_input_tokens") or 0)
                        h.session_writes[key] += (
                            (usage.get("cache_creation_input_tokens") or 0)
                            + (usage.get("input_tokens") or 0))
                        if ctx > HIGH_CONTEXT_TOKENS:
                            h.session_high_turns[key] = h.session_high_turns.get(key, 0) + 1
                            h.session_carried[key] = (h.session_carried.get(key, 0)
                                                      + ctx - HIGH_CONTEXT_TOKENS)
                            run = h.session_cur_run.get(key, 0) + 1
                            h.session_cur_run[key] = run
                            if run > h.session_high_run.get(key, 0):
                                h.session_high_run[key] = run
                        else:
                            h.session_cur_run[key] = 0
                        if is_sub:
                            h.subagent_turns += 1
                            h.subagent_models[model] += 1
                        elif entry.get("isSidechain"):
                            h.sidechain_turns += 1
                elif msg.get("role") == "assistant":
                    h.missing_usage += 1

                content = msg.get("content")
                if isinstance(content, list):
                    # Tool NAMES are inventory, not activity: collect them from
                    # all history so the A6 estimate has a real sample, even
                    # for turns outside the reporting window.
                    for block in content:
                        if (isinstance(block, dict)
                                and block.get("type") == "tool_use"
                                and str(block.get("name") or "").startswith("mcp__")):
                            parts = str(block["name"]).split("__")
                            if len(parts) > 2:
                                h.mcp_tools_seen[parts[1]].add(parts[2])
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
                        if name in ("Task", "Agent") and inp.get("subagent_type"):
                            h.agent_invocations[str(inp["subagent_type"])] += 1
                        if name == "Read" and inp.get("file_path"):
                            key = str(inp["file_path"])
                            if key.lower().endswith(
                                    (".png", ".jpg", ".jpeg", ".gif", ".webp",
                                     ".bmp", ".svg")):
                                h.image_reads += 1
                            # A ranged Read copies only part of the file;
                            # charging the whole thing overstated a partial
                            # read of a large file by an order of magnitude.
                            try:
                                size = os.path.getsize(key)
                            except OSError:
                                size = 0
                            limit = inp.get("limit")
                            if isinstance(limit, int) and limit > 0:
                                size = min(size, limit * 80)  # ~80 chars/line
                            reads["__last_size__"] = size
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
            + c["cache_read"] * pin * cache_read_mult(model)
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
    reads = tokens_per_turn * h.turns * blended_cache_read_rate(h) / 1e6
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


AGENTS_NAMES = ("AGENTS.md",)


def scan_agents_md(cwd, entries):
    """AGENTS.md is the cross-tool instructions file other coding agents read.
    Claude Code reads it too, but only when there is **no** CLAUDE.md or
    CLAUDE.local.md in the working directory or above it - a CLAUDE.md always
    wins. So the same file is either always-on overhead or completely inert,
    and which one depends on what else is in the directory chain.

    The user-scope ~/.claude/CLAUDE.md does not count: if it did, anyone with
    a global instructions file could never have an AGENTS.md read, which
    would make the feature pointless.
    """
    chain_claude = [e for e in entries if e["role"] in ("project", "ancestor")]
    found = []
    seen = set()
    root = os.path.realpath(cwd or os.getcwd())
    here = root
    while here and here != os.path.dirname(here):
        for name in AGENTS_NAMES:
            path = os.path.join(here, name)
            real = os.path.realpath(path)
            if real in seen or not os.path.isfile(path):
                continue
            seen.add(real)
            try:
                with open(path, encoding="utf-8", errors="replace") as fh:
                    text = fh.read()
            except OSError:
                continue
            found.append({"path": path, "bytes": len(text),
                          "lines": text.count("\n") + 1 if text else 0})
        if here == HOME:
            break
        here = os.path.dirname(here)
    return {"files": found, "loaded": not chain_claude}


def dead_agents_md(known_cwds, limit=20):
    """AGENTS.md files Claude never reads, machine-wide.

    Not a cost finding - these cost nothing precisely because they are
    ignored. It is a correctness one: someone maintaining an AGENTS.md whose
    rules are silently not being followed wants to know.
    """
    dead = []
    seen = set()
    for d in sorted(known_cwds or []):
        if not os.path.isdir(d):
            continue
        agents = os.path.join(d, "AGENTS.md")
        real = os.path.realpath(agents)
        if real in seen or not os.path.isfile(agents):
            continue
        seen.add(real)
        # Walk up looking for a CLAUDE.md that would win.
        here, blocker = os.path.realpath(d), None
        while here and here != os.path.dirname(here):
            for n in ("CLAUDE.md", "CLAUDE.local.md",
                      os.path.join(".claude", "CLAUDE.md")):
                cand = os.path.join(here, n)
                if os.path.isfile(cand):
                    blocker = cand
                    break
            if blocker or here == HOME:
                break
            here = os.path.dirname(here)
        if blocker:
            try:
                lines = sum(1 for _ in open(agents, encoding="utf-8",
                                            errors="replace"))
            except OSError:
                lines = 0
            dead.append({"path": agents, "lines": lines, "blocked_by": blocker})
    return dead[:limit]


RULES_PATHS_RE = re.compile(r"^\s*paths\s*:", re.M)


def scan_rules(cwd):
    """`.claude/rules/*.md` - the mechanism the docs actually recommend for a
    long CLAUDE.md, and the one this skill never looked at.

    A rule **without** `paths:` frontmatter loads at launch, same as
    CLAUDE.md, so it is always-on overhead. A rule **with** `paths:` loads
    only when Claude touches a matching file, so it is free until used. That
    distinction is the whole point, and pricing them alike would be wrong in
    both directions.
    """
    out = {"always": [], "scoped": []}
    roots = [os.path.join(CLAUDE_DIR, "rules"),
             os.path.join(os.path.realpath(cwd or os.getcwd()), ".claude", "rules")]
    seen = set()
    for root in roots:
        if not os.path.isdir(root):
            continue
        for path in sorted(glob.glob(os.path.join(root, "**", "*.md"),
                                     recursive=True)):
            real = os.path.realpath(path)
            if real in seen:
                continue
            seen.add(real)
            try:
                with open(path, encoding="utf-8", errors="replace") as fh:
                    text = fh.read()
            except OSError:
                continue
            fm = parse_frontmatter(text)
            entry = {"path": path, "bytes": len(text),
                     "lines": text.count("\n") + 1 if text else 0}
            if fm and RULES_PATHS_RE.search(fm):
                out["scoped"].append(entry)
            else:
                out["always"].append(entry)
    return out


def check_claude_md(entries, h, wmult, elsewhere=None, cwd=None,
                    settings=None):
    """A1/A2, rewritten.

    A1 was a machine-wide sum: every project's oversized CLAUDE.md added
    together and priced as if all of them loaded on every message. You are
    only ever in one project. A7 already had the right pattern - price the
    place you are, list the rest - and this now copies it.

    A2 used to fire whenever any ancestor CLAUDE.md existed. But nested
    CLAUDE.md files are the recommended layout: a short root file plus
    per-subfolder context that loads on demand. Once you work inside the
    subfolder its parents are ancestors, so the old check flagged a
    well-organised setup as waste. It is inverted here: the problem is not
    too many files, it is a root file carrying rules that belong in one
    subfolder.
    """
    findings = []
    excludes = claude_md_excludes(settings)
    loaded = [e for e in entries if e["role"] in ("global", "project", "ancestor")]
    skipped = [e for e in loaded if _excluded(e["path"], excludes)]
    loaded = [e for e in loaded if e not in skipped]

    # AGENTS.md counts only when no CLAUDE.md in the chain outranks it, and
    # .claude/rules files count only when they have no `paths:` scope.
    agents = scan_agents_md(cwd, entries)
    if agents["loaded"]:
        loaded = loaded + agents["files"]
    rules = scan_rules(cwd)
    loaded = loaded + rules["always"]

    total_tok = sum(e["bytes"] for e in loaded) / CHARS_PER_TOKEN
    total_lines = sum(e["lines"] for e in loaded)

    if not loaded:
        findings.append(Finding("A1", "Instructions loaded on every message", "N/A",
                                evidence="you do not have any CLAUDE.md instruction files yet"))
    elif total_lines > threshold("claude_md_lines"):
        findings.append(Finding(
            "A1", "Instructions loaded on every message", "FIX",
            evidence=f"{total_lines} lines across {len(loaded)} file(s) load on "
                     "every message here: "
                     + "; ".join(f"{_short(e['path'])} ({e['lines']})"
                                 for e in sorted(loaded, key=lambda x: -x["lines"])[:5]),
            why="CLAUDE.md is your instructions file. Claude re-reads it on "
                "every single message, so every line costs you all day. In this "
                "folder several stack up: your global one, the one here, and "
                "one in every folder above. They are added together. A "
                "CLAUDE.md inside a sub-folder is different - that one is only "
                "read if Claude opens a file in that sub-folder. Aim for under "
                "200 lines in total.",
            fix=("Your " + str(len(rules["always"])) + " always-on rule file(s) "
                 "in .claude/rules/ would load only when needed if you gave "
                 "them a 'paths:' line. " if rules["always"] else "")
                + "Move rules that only matter in one sub-folder into a CLAUDE.md "
                "there, so they load only when relevant. For rules that apply "
                "to particular file types, put them in .claude/rules/ with a "
                "'paths:' line - those load only when Claude touches a matching "
                "file. Delete anything out of date.",
            can_fix="ASSISTED", doc=DOCS["memory"],
            tokens_per_turn=total_tok,
            monthly_usd=overhead_monthly_usd(total_tok, h, wmult)))
    else:
        nested = "" 
        findings.append(Finding(
            "A1", "Instructions loaded on every message", "PASS",
            evidence=f"{total_lines} lines across {len(loaded)} file(s) "
                     f"(~{int(total_tok)} tokens) - under the 200-line guideline"))

    if skipped:
        findings.append(Finding(
            "A1c", "Instruction files you have excluded", "PASS",
            evidence=f"{len(skipped)} file(s) skipped by claudeMdExcludes: "
                     + "; ".join(_short(e["path"]) for e in skipped[:4])
                     + " - not loaded, so not charged"))

    # Machine-wide view: real, but not a cost you pay here. Unpriced.
    big_elsewhere = [e for e in (elsewhere or []) if e["lines"] > 200]
    if big_elsewhere:
        findings.append(Finding(
            "A1b", "Long instruction files in other projects", "N/A",
            evidence=f"{len(big_elsewhere)} file(s) over 200 lines elsewhere: "
                     + "; ".join(f"{_short(e['path'])} ({e['lines']})"
                                 for e in big_elsewhere[:5]),
            why="These load when you work in those projects, not in this one, "
                "so they cost you nothing right now. Listed so you know they "
                "are there - they are not counted in the total above.",
            fix="Run the audit from those folders to see what they cost there.",
            can_fix="NONE", doc=DOCS["memory"]))

    # ---- A2, inverted: is the always-on file doing sub-folder work? -----
    project = [e for e in entries if e["role"] == "project"]
    proj_lines = sum(e["lines"] for e in project)
    subdirs, nested_files = set(), 0
    root = os.path.realpath(cwd or os.getcwd())
    for d in (h.cwds or set()):
        try:
            rd = os.path.realpath(d)
        except OSError:
            continue
        if rd != root and rd.startswith(root + os.sep):
            subdirs.add(rd)
            for cand in (os.path.join(rd, "CLAUDE.md"),
                         os.path.join(rd, ".claude", "CLAUDE.md")):
                if os.path.isfile(cand):
                    nested_files += 1
    rules_dir = os.path.join(root, ".claude", "rules")
    has_rules = os.path.isdir(rules_dir) and bool(
        glob.glob(os.path.join(rules_dir, "**", "*.md"), recursive=True))

    if not project:
        findings.append(Finding("A2", "Instructions in the wrong place", "N/A",
                                evidence="no project instructions file here"))
    elif nested_files or has_rules:
        findings.append(Finding(
            "A2", "Instructions in the wrong place", "PASS",
            evidence=f"project file is {proj_lines} lines and you also have "
                     + (f"{nested_files} sub-folder CLAUDE.md file(s)" if nested_files else "")
                     + (" and " if nested_files and has_rules else "")
                     + ("path-scoped rules in .claude/rules/" if has_rules else "")
                     + " - context is split so it loads only when relevant"))
    elif (proj_lines > threshold("project_file_lines")
          and len(subdirs) >= threshold("active_subdirs")):
        # Unit rate, not a total: we cannot know which rules are splittable,
        # so quoting a saving would be inventing one.
        per_100 = overhead_monthly_usd(100 * 80 / CHARS_PER_TOKEN, h, wmult)
        findings.append(Finding(
            "A2", "Instructions in the wrong place", "FIX",
            evidence=f"your project file is {proj_lines} lines and you have "
                     f"worked in {len(subdirs)} sub-folders here, none of which "
                     "has its own instructions file",
            why="Rules that only matter in one part of the project are being "
                "paid for in all of it, on every message. A CLAUDE.md inside a "
                "sub-folder only loads when Claude opens a file there, and a "
                "file in .claude/rules/ with a 'paths:' line only loads when "
                "Claude touches a matching file. Splitting is the recommended "
                "layout, not a workaround.",
            fix="Show me the file and I will point out which rules name a "
                "single sub-folder, then move them. Every 100 lines moved down "
                f"saves roughly {money(per_100)} a month here.",
            can_fix="ASSISTED", doc=DOCS["memory"]))
    else:
        findings.append(Finding(
            "A2", "Instructions in the wrong place", "PASS",
            evidence=f"project file is {proj_lines} lines across "
                     f"{len(subdirs)} active sub-folder(s) - small enough that "
                     "splitting it would not pay off"))

    # A3b - correctness, not cost. These files cost nothing precisely because
    # they are ignored, which is the problem: someone maintaining one thinks
    # its rules are being followed.
    dead = dead_agents_md(h.cwds)
    if dead:
        findings.append(Finding(
            "A3b", "Instruction files Claude never reads", "FIX",
            evidence=f"{len(dead)} AGENTS.md file(s) are being ignored: "
                     + "; ".join(f"{_short(d['path'])} ({d['lines']} lines, "
                                 f"outranked by {_short(d['blocked_by'])})"
                                 for d in dead[:4]),
            why="AGENTS.md is the instructions file other coding tools read. "
                "Claude reads it too, but only when there is no CLAUDE.md in "
                "that folder or any folder above it - a CLAUDE.md always wins. "
                "These cost you nothing, because Claude never opens them. That "
                "is the problem: if you wrote rules in them expecting Claude to "
                "follow them, it is not.",
            fix="Either move the rules you still want into the CLAUDE.md that "
                "is winning, or add a line '@AGENTS.md' to that CLAUDE.md to "
                "pull them in. Note the second option costs tokens on every "
                "message, because imports load at launch.",
            can_fix="ASSISTED", doc=DOCS["memory"]))
    elif h.cwds:
        findings.append(Finding("A3b", "Instruction files Claude never reads",
                                "PASS",
                                evidence="no ignored AGENTS.md files"))

    # A3c - path-scoped rules are the recommended pattern; say so when used.
    if rules["scoped"]:
        findings.append(Finding(
            "A3c", "Rules that load only when needed", "PASS",
            evidence=f"{len(rules['scoped'])} rule file(s) in .claude/rules/ "
                     "are scoped with 'paths:', so they cost nothing until "
                     "Claude touches a matching file"))

    imports = [(e, i) for e in entries for i in e["imports"]]
    if imports:
        tok = sum(i["bytes"] for _, i in imports) / CHARS_PER_TOKEN
        findings.append(Finding(
            "A3", "Imported files in CLAUDE.md", "FIX",
            evidence="; ".join(
                f"{_short(e['path'])} -> @{i['spec']} ({i['bytes']}B)"
                + ("" if i.get("depth", 1) == 1
                   else f" [{i['depth']} hops deep]")
                for e, i in imports),
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
    # Two tiers. Only a skill old enough to have had the full window to prove
    # itself is priced or auto-fixed; a younger one is reported with its real
    # age and nothing else. Saying "not used in 30 days" about a 9-day-old
    # skill is simply false, and this tool is sold on not doing that.
    stale, young = [], []
    for s in personal:
        if _invoked(s["name"], None, h.skill_invocations):
            continue
        win = unused_window(h, window_days, s["mtime"])
        if win >= window_days - 0.5:
            stale.append((s, win))
        elif win >= 7:
            young.append((s, win))

    if not h.turns:
        findings.append(Finding("A4", "Skills you never use", "UNKNOWN",
                                evidence="no session history to look at yet"))
    elif stale:
        tok = sum(s["bytes"] for s, _ in stale) / CHARS_PER_TOKEN
        extra = ""
        if young:
            extra = ("; also " + ", ".join(f"{s['name']} (new, {w:.0f}d old)"
                                           for s, w in young[:3])
                     + " - too new to judge, not counted")
        findings.append(Finding(
            "A4", "Skills you never use", "FIX",
            evidence=f"{len(stale)} skills you have not used in {window_days} days: "
                     + ", ".join(s["name"] for s, _ in stale[:8]) + extra,
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
            payload={"paths": [s["path"] for s, _ in stale]}))
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
        # Same two-tier rule as A4: only judge a plugin that has had the whole
        # window to be used.
        if unused_window(h, window_days, newest) < window_days - 0.5:
            continue
        if any(_invoked(s["name"], plugin, h.skill_invocations) for s in group):
            continue
        # An agent or MCP tool from this plugin counts as using the plugin.
        if any(k == plugin or k.startswith(f"{plugin}:")
               for k in h.agent_invocations):
            continue
        if any(plugin.lower() in used.lower() or used.lower() in plugin.lower()
               for used in h.mcp_used):
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
            fix="Hide just the skills you do not use, which keeps the "
                "plugin's agents and tools working - add them to "
                "'skillOverrides' in your settings. Only turn the whole plugin "
                "off if you use none of it: "
                + "; ".join(f"/plugin disable {p}" for p in names[:4]),
            # ASSISTED, not AUTO: "unused" is inferred from invocations we can
            # see, and a plugin's hooks leave no trace at all. Silently
            # disabling a plugin someone relies on is not a reversible
            # mechanical edit in the sense the AUTO contract promises.
            can_fix="ASSISTED", doc=DOCS["plugins"],
            tokens_per_turn=tok, monthly_usd=overhead_monthly_usd(tok, h, wmult),
            payload={"plugins": names, "enabled_keys": list(enabled_plugins or {})}))
    else:
        findings.append(Finding("A5", "Plugins you never use", "PASS",
                                evidence=f"{len(by_plugin)} plugins, all either used or already off"))
    return findings


# An MCP server you actually use still carries its tool names on every
# message. A CLI does not - Claude just runs it. So the swap is worth more on
# a server you USE than on one you have already been told to disconnect.
CLI_ALTERNATIVES = {
    "github": "gh", "atlassian": "acli", "jira": "acli",
    "confluence": "acli", "gitlab": "glab", "aws": "aws",
    "gcloud": "gcloud", "google-cloud": "gcloud", "sentry": "sentry-cli",
    "docker": "docker", "kubernetes": "kubectl", "k8s": "kubectl",
    "stripe": "stripe", "vercel": "vercel", "netlify": "netlify",
    "heroku": "heroku", "supabase": "supabase", "linear": "linear",
    "cloudflare": "wrangler", "npm": "npm", "postgres": "psql",
}


def _cli_for(server):
    """The CLI that replaces this server, if one is known."""
    low = server.lower()
    for key, cli in CLI_ALTERNATIVES.items():
        if key in low:
            return cli
    return None



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
    here_tok = None          # the project the user is actually in
    here_problem = None
    for mem_dir in dirs:
        project = os.path.basename(os.path.dirname(mem_dir))
        index = os.path.join(mem_dir, "MEMORY.md")
        files = [q for q in glob.glob(os.path.join(mem_dir, "*.md"))
                 if os.path.basename(q) != "MEMORY.md"]
        if not os.path.isfile(index):
            if files:
                problems.append(f"{_tail(project, known_cwds=h.cwds)}: {len(files)} memory file(s) "
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
        # Only the first 200 lines or 25KB of MEMORY.md load, whichever comes
        # first. Pricing the whole file bills for text that never reaches
        # Claude - and the overflow is a correctness problem, not a cost one.
        MEM_BYTE_LIMIT = 25 * 1024
        head = "\n".join(text.splitlines()[:MEMORY_LINE_COUNT])[:MEM_BYTE_LIMIT]
        dropped_lines = max(0, len(text.splitlines()) - MEMORY_LINE_COUNT)
        dropped_bytes = max(0, len(text) - MEM_BYTE_LIMIT)
        tok = len(head) / CHARS_PER_TOKEN
        is_here = _slug_matches(mem_dir, cwd) if cwd else False
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
        if dropped_lines or dropped_bytes:
            # Lead with the consequence, not the size: these memories are
            # silently never loaded.
            bits.append(f"{len(lines)} lines - everything past line "
                        f"{MEMORY_LINE_COUNT} (or 25KB) is dropped and never "
                        "reaches Claude")
        if long_lines:
            bits.append(f"{len(long_lines)} line(s) too long")
        if orphans:
            bits.append(f"{len(orphans)} memory file(s) missing from the list")
        if broken:
            bits.append(f"{len(broken)} link(s) pointing at a deleted file")
        if is_here:
            here_tok = tok
        if bits:
            line = (f"{_tail(project, known_cwds=h.cwds)} (~{int(tok)} tokens per message): "
                    + ", ".join(bits))
            problems.append(line)
            if is_here:
                here_problem = line

    if not indexes and not problems:
        return [Finding("A7", "Memory notes too long", "N/A",
                        evidence="no memories stored")]
    if not problems:
        return [Finding(
            "A7", "Memory notes too long", "PASS",
            evidence=f"{indexes} MEMORY.md file(s), largest ~{int(worst_tok)} "
                     f"tokens per message in its project, all entries indexed and short")]
    # Price the project the user is IN. Each MEMORY.md is per-turn cost in
    # its own project, so summing across projects double-counts a cost nobody
    # pays at once - and the worst project elsewhere is not what this session
    # is paying either. Falls back to the worst when the current project
    # cannot be identified.
    return [Finding(
        "A7", "Memory notes too long", "FIX",
        evidence=("priced for this project" if here_tok is not None
                  else "priced for your largest project - this one has no "
                       "memory list") + f"; {len(problems)} of your {indexes} "
                 "project memory lists need tidying - "
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
        tokens_per_turn=(here_tok if here_tok is not None else worst_tok),
        monthly_usd=overhead_monthly_usd(
            here_tok if here_tok is not None else worst_tok, h, wmult))]


def _tail(text, n=26, known_cwds=None):
    """A readable name for a project slug.

    Slugs are the cwd with separators replaced, so splitting on '-' is lossy
    when folder names contain dashes or spaces. Where the real directory is
    known from transcripts we use its last two components; otherwise we
    truncate visibly rather than silently producing 'rar-Documents-Private'.
    """
    for d in (known_cwds or ()):
        try:
            if _slug_matches_slug(text, d):
                parts = [x for x in os.path.realpath(d).split(os.sep) if x]
                return os.path.join(*parts[-2:]) if len(parts) >= 2 else d
        except OSError:
            continue
    return text if len(text) <= n else "..." + text[-n:]


def _slug_matches_slug(slug, cwd):
    norm = lambda x: re.sub(r"[^a-z0-9]", "", x.lower())
    return norm(slug) == norm(cwd)


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
    average extrapolates to the idle ones.

    Two things keep this honest. The sample is drawn from ALL history, not the
    reporting window - a 30-day window sees a handful of calls per server and
    would learn "3 tools per server" for a server that publishes 70. And the
    result is a RANGE: the low end is the observed average, the high end the
    largest server actually seen. Real servers vary by an order of magnitude,
    so a single number here is false precision.
    """
    per_server = {k: set(v) for k, v in h.mcp_tools_seen.items() if v}
    if not per_server:
        # Fall back to the windowed counter if the wider sweep found nothing.
        per_server = collections.defaultdict(set)
        for name in h.tool_calls:
            if name.startswith("mcp__"):
                parts = name.split("__")
                if len(parts) > 2:
                    per_server[parts[1]].add(parts[2])
        per_server = {k: v for k, v in per_server.items() if v}
    if not per_server:
        return None
    counts = [len(v) for v in per_server.values()]
    tools_avg = sum(counts) / len(counts)
    tools_max = max(counts)
    chars_per = (sum(len(t) for v in per_server.values() for t in v)
                 / max(sum(counts), 1))
    per_name_tok = chars_per / CHARS_PER_TOKEN
    return {"tokens": unused_count * tools_avg * per_name_tok,
            "tokens_high": unused_count * tools_max * per_name_tok,
            "tools_per_server": round(tools_avg, 1),
            "tools_max": tools_max,
            "servers_sampled": len(per_server),
            "chars_per_name": round(chars_per, 1)}


def check_mcp(configured, h, window_days=30, wmult=WRITE_MULT_1H):
    """A6. `configured` is the dict from mcp_servers(): only the "active" set
    loads in this session and may be priced."""
    if isinstance(configured, dict):
        active = configured.get("active") or set()
        other = configured.get("other_projects") or {}
        needs_auth = configured.get("needs_auth") or set()
    else:  # tolerate the old set-only signature
        active, other, needs_auth = set(configured or ()), {}, set()

    findings = []
    if other:
        findings.append(Finding(
            "A6b", "Connected tools set up in other projects", "N/A",
            evidence=f"{len(other)} server(s) configured elsewhere, not loaded here: "
                     + ", ".join(sorted(other)[:8]),
            why="A service attached to one project only loads in that project's "
                "folder, and it does not carry into subfolders either. These cost "
                "you nothing in this session, so they are listed for awareness "
                "and not counted in the total.",
            fix="Nothing to do unless you no longer use the project it belongs to.",
            can_fix="NONE", doc=DOCS["mcp"]))

    if not active:
        findings.append(Finding("A6", "Connected tools you never use", "N/A",
                                evidence="you have no outside services connected in this folder"))
        return findings
    if not h.turns:
        findings.append(Finding("A6", "Connected tools you never use", "UNKNOWN",
                                evidence="no session history to look at yet"))
        return findings

    # A6c - servers you DO use that have a free command-line equivalent.
    swappable = []
    for name in sorted(active & h.mcp_used):
        cli = _cli_for(name)
        if cli:
            swappable.append((name, cli, shutil.which(cli) is not None))
    if swappable:
        # For a server we USE we do not have to extrapolate: its own tool
        # names appear in the transcripts. Still a floor - only tools actually
        # called are visible - but a measured one rather than an average.
        own_tok = 0.0
        for n, _, _ in swappable:
            names = h.mcp_tools_seen.get(n) or set()
            own_tok += sum(len(t) for t in names) / CHARS_PER_TOKEN
        est_all = mcp_name_overhead(h, len(swappable))
        if est_all and own_tok > est_all["tokens"]:
            est_all = dict(est_all, tokens=own_tok,
                           tokens_high=max(own_tok, est_all["tokens_high"]))
        installed = [c for _, c, ok in swappable if ok]
        findings.append(Finding(
            "A6c", "Connected tools with a free command-line version", "FIX",
            evidence="; ".join(
                f"{n} -> {c}" + ("" if ok else f" ({c} not installed)")
                for n, c, ok in swappable)
            + (f" (~{fmt_tok(est_all['tokens'])}-{fmt_tok(est_all['tokens_high'])}"
               " tokens per message est.)" if est_all else ""),
            why="These services are connected as MCP servers, which means their "
                "tool names sit in the list Claude carries on every message - "
                "even when you are not using them. The same jobs can be done "
                "with a command-line tool, which Claude simply runs when needed "
                "and which adds nothing to every message. You are using these, "
                "so this is a swap rather than a disconnect."
                + (" The command-line versions are already installed on this "
                   "machine." if installed and len(installed) == len(swappable)
                   else ""),
            fix="Try the command-line tool for a week - ask Claude to use "
                + ", ".join(sorted({c for _, c, _ in swappable}))
                + " instead. If you do not miss the server, disconnect it.",
            can_fix="MANUAL", doc=DOCS["mcp"],
            tokens_per_turn=est_all["tokens"] if est_all else 0,
            monthly_usd=(overhead_monthly_usd(est_all["tokens"], h, wmult)
                         if est_all else 0.0),
            payload={"swaps": [{"server": n, "cli": c, "installed": ok}
                               for n, c, ok in swappable]}))

    # Servers we can see being used but which are not in the config files we
    # read: plugin-bundled ones, and claude.ai connectors. Calling them all
    # "plugins" would be wrong - what is true is that they load without
    # appearing in the user's own connection list.
    unlisted = sorted(n for n in h.mcp_tools_seen
                      if n not in active and n not in other)
    if unlisted:
        findings.append(Finding(
            "A6d", "Connected tools not in your config file", "N/A",
            evidence=f"{len(unlisted)} server(s) in use but not listed in your "
                     "settings: " + ", ".join(unlisted[:6])
                     + (f" (+{len(unlisted) - 6} more)" if len(unlisted) > 6 else "")
                     + " - not counted in the totals",
            why="These load from somewhere this audit cannot read - a plugin "
                "that bundles its own service, or a connector set up in the "
                "Claude app. They add tool names to every message like any "
                "other server, but we cannot size them or switch them off "
                "from here.",
            fix="Run /mcp to see every server this session has, and turn off "
                "any you do not use. For one that came with a plugin, turning "
                "off the plugin is the switch.",
            can_fix="MANUAL", doc=DOCS["mcp"]))

    unused = sorted(s for s in active if s not in h.mcp_used)
    if not unused:
        findings.append(Finding("A6", "Connected tools you never use", "PASS",
                                evidence=f"all {len(active)} connected services were actually used"))
        return findings

    est = mcp_name_overhead(h, len(unused))
    swaps = []
    fix = ("Disconnect the servers you are not using. You can always reconnect "
           "one later if you need it.")
    if swaps:
        fix += (" These also have a command-line tool that does the same job for "
                "free, with no always-on cost: " + ", ".join(sorted(set(swaps))))

    pending = sorted(n for n in unused if n in needs_auth)
    if pending:
        fix += (" " + ", ".join(pending) + " never finished signing in, so "
                + ("they are" if len(pending) > 1 else "it is")
                + " costing you nothing right now - either finish the sign-in "
                  "or remove it.")

    rng = ""
    if est:
        rng = (f" (~{fmt_tok(est['tokens'])}-{fmt_tok(est['tokens_high'])} "
               f"tokens per message est.)")

    findings.append(Finding(
        "A6", f"Connected tools not used in {window_days}d", "FIX",
        evidence=f"{len(unused)} of {len(active)} unused: "
                 + ", ".join(unused[:8]) + rng,
        why="An MCP server is an outside service you connect to Claude, like "
            "Gmail or Jira. Its tool instructions only load when needed now, but "
            "the tool names still sit in the list Claude carries on every "
            "message. Anthropic does not publish that cost, so this is our own "
            "estimate from the servers you do use"
            + (f" ({est['servers_sampled']} sampled, {est['tools_per_server']} "
               f"tools each on average and {est['tools_max']} on the largest, "
               f"{est['chars_per_name']} characters per name). It is a range, "
               "not a measurement, and it is an upper bound in one more way: a "
               "server that fails to start registers no tools and costs nothing, "
               "which we cannot tell apart from one you simply never called."
               if est else "."),
        fix=fix, can_fix="MANUAL", doc=DOCS["mcp"],
        tokens_per_turn=est["tokens"] if est else 0,
        monthly_usd=(overhead_monthly_usd(est["tokens"], h, wmult) if est else 0.0),
        payload={"unused": unused, "needs_auth": pending,
                 "tokens_high": est["tokens_high"] if est else 0}))
    return findings


def check_fast_mode(h, wmult):
    """B4. Fast mode runs the same model at up to 2.5x the output speed for
    roughly double the price. It is toggled with /fast and persists, so it is
    easy to leave on without noticing."""
    if not h.turns:
        return []
    if not h.fast_turns:
        return [Finding("B4", "Fast mode left on", "PASS",
                        evidence="no messages used fast mode")]
    share = h.fast_turns / h.turns * 100
    extra, unpriced = 0.0, []
    for model, c in h.by_model.items():
        if not model.endswith(FAST_SUFFIX):
            continue
        base = _base_model(model)
        if base not in FAST_PRICES:
            unpriced.append(base)
            continue
        fast, std = FAST_PRICES[base], PRICES.get(base)
        if not std:
            continue
        writes = c["w5m"] * WRITE_MULT_5M + (c["w1h"] + c["w_unknown"]) * WRITE_MULT_1H
        reads = c["cache_read"] * cache_read_mult(model)
        extra += ((c["in"] + writes + reads) * (fast[0] - std[0])
                  + c["out"] * (fast[1] - std[1])) / 1e6
    note = ""
    if unpriced:
        note = (" Anthropic has not published a fast-mode price for "
                + ", ".join(sorted(set(unpriced)))
                + ", so those messages are priced at the standard rate and this "
                  "figure is a floor.")
    return [Finding(
        "B4", "Fast mode left on", "FIX",
        monthly_usd=extra, kind="measured",
        evidence=f"{h.fast_turns} of {h.turns} messages ({share:.0f}%) ran in "
                 "fast mode",
        why="Fast mode is the same model answering quicker, at about double "
            "the price. You turn it on with /fast and it stays on, so it is "
            "easy to leave running long after the thing you were waiting for."
            + note,
        fix="Type /fast to turn it off when you are not waiting on the answer. "
            "Turn it back on for the times speed actually matters.",
        can_fix="MANUAL", doc=DOCS["model"])]


def check_models(h, settings, sources):
    findings = []
    if not h.turns:
        return [Finding("B1", "Most work runs on the expensive model", "UNKNOWN",
                        evidence="no session history to look at yet"),
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
                    + c["cache_read"] * sonnet_in * cache_read_mult("claude-sonnet-5")
                    + writes * sonnet_in) / 1e6

    model_setting = settings.get("model")
    if share > threshold("expensive_share") and exp_cost > alt:
        # Was (exp_cost - alt) / 2. The halving had no source and contradicted
        # this skill's own rules: figures come from arithmetic, and category B
        # is quoted as an upper bound. Report the bound, labelled.
        upper = exp_cost - alt
        where = f" (set in {sources.get('model', '?')} settings)" if model_setting else ""
        findings.append(Finding(
            "B1", "Most work runs on the expensive model", "FIX",
            evidence=f"{share*100:.0f}% of your messages went to an expensive model, costing "
                     f"{money(exp_cost)} of {money(sum(costs.values()))} in this period"
                     + (f"; model={model_setting}{where}" if model_setting else ""),
            why="This check counts how much of your work ran on the costly "
                "model - it cannot tell which of it was hard, so read the "
                "figure as a question rather than a verdict. Claude Code "
                "fixes the model when a session starts and never switches by "
                "itself, so your priciest model also handles 'rename this "
                "variable'. The same work at Sonnet's rates would have cost "
                + money(alt) + " - a best case, since it assumes Sonnet could "
                "have finished it - so treat this as the most it could save, "
                "not a forecast. Switch at the start of a session, not "
                "mid-way: changing model discards the cached conversation, so "
                "all of it gets re-sent once at full price.",
            fix=("Type /model sonnet at the start of a session you know is easy"
                 + (", or change your default `model` setting. "
                    if model_setting else ". ")
                 + "You can also turn on a reminder that speaks up on your first "
                   "message, and only when the task clearly does not need what "
                   f'you are paying for: `python3 "{SELF_PATH}" --install-hook`'),
            can_fix="ASSISTED", doc=DOCS["model"], monthly_usd=upper, kind="bound",
            payload={"model_setting": model_setting,
                     "upper_bound_usd": round(exp_cost - alt, 2),
                     "action": "install_hook"}))
    else:
        findings.append(Finding("B1", "Most work runs on the expensive model", "PASS",
                                evidence=f"only {share*100:.0f}% of messages used an expensive model"))

    # Effort scale, cheapest first. Claude Code's own default sits at the
    # expensive end, so "the user never set it" is the most common costly
    # case - not a pass. Not hardcoded as a threshold: the rule is "anything
    # above medium", and the default is named so it can be corrected when the
    # platform changes it.
    EFFORT_ORDER = ["low", "medium", "high", "xhigh", "max"]
    PLATFORM_DEFAULT_EFFORT = "xhigh"
    effort = settings.get("effortLevel")
    effective = effort or PLATFORM_DEFAULT_EFFORT
    from_default = effort is None

    if effective not in EFFORT_ORDER:
        findings.append(Finding("B3", "Thinking effort above medium", "UNKNOWN",
                                evidence=f"effort is set to '{effective}', which this "
                                         "version does not recognise"))
    elif EFFORT_ORDER.index(effective) <= EFFORT_ORDER.index("medium"):
        findings.append(Finding(
            "B3", "Thinking effort above medium", "PASS",
            evidence=f"effort is '{effective}' - at or below medium, nothing to save here"))
    else:
        # Effort acts on reasoning and output tokens, so output spend is the
        # bulk of what lowering it can reach. It also tends to produce fewer,
        # more consolidated tool calls, so the true figure can exceed this -
        # it is an estimate, not a ceiling.
        out_cost = 0.0
        for model, c in h.by_model.items():
            pr = price_for(model)
            if pr:
                out_cost += c["out"] * pr[1] / 1e6
        # One step down from xhigh saves more than one step down from high.
        steps = EFFORT_ORDER.index(effective) - EFFORT_ORDER.index("medium")
        assumed_cut = min(threshold("effort_cut_per_step") * steps, 0.45)
        where = (f" (in your {sources.get('effortLevel','?')} settings)"
                 if not from_default else
                 " - you have not set this, so you are on Claude Code's default")
        findings.append(Finding(
            "B3", "Thinking effort above medium", "FIX",
            evidence=f"effort is '{effective}'{where}",
            why="Effort is how long Claude thinks before answering, and you pay "
                "for that thinking. It is set once per session, so it applies to "
                "simple jobs too. Dropping effort usually costs less quality "
                "than dropping to a weaker model, so try it first. Thinking and "
                "answers cost " + money(out_cost) + " here; we assume a "
                f"{int(assumed_cut*100)}% cut going from '{effective}' to "
                "'medium', which is an estimate rather than a measurement."
                + (" Note you are already below the default, so this is the "
                   "second step down." if not from_default
                      and EFFORT_ORDER.index(effective)
                      < EFFORT_ORDER.index(PLATFORM_DEFAULT_EFFORT) else ""),
            fix="Set your default effort to medium with /effort or in /model, "
                "and raise it for the occasional hard task that needs it.",
            can_fix="ASSISTED", doc=DOCS["settings"], kind="bound",
            monthly_usd=out_cost * assumed_cut,
            payload={"effortLevel": effective, "from_default": from_default,
                     "output_cost_usd": round(out_cost, 2),
                     "assumed_reduction": assumed_cut}))
    return findings


def _check_long_sessions(h):
    out = []
    # Repointed twice. The original tuned autoCompactEnabled/autoCompactWindow;
    # the docs name the real cause - "long sessions that were never cleared".
    #
    # SELECTION is by time spent above the line, not by peak. Peak is the size
    # of one message's history: a session that touched 400k on its final
    # message cost almost nothing extra, while one sitting at 300k for 400
    # messages paid that every time. Peak ranks those two the same.
    #
    # PRICING uses the cache_read actually recorded for the session, which is
    # what was really billed to carry that history, rather than anything
    # derived from peak.
    if not h.session_peak:
        out.append(Finding("C2", "Long sessions never cleared", "N/A",
                                evidence="no session sizes recorded"))
        return out

    peaks = sorted(h.session_peak.values(), reverse=True)
    heavy = [k for k in h.session_peak
             if h.session_high_run.get(k, 0) >= HIGH_CONTEXT_MIN_RUN]
    if not heavy:
        longest = max(h.session_high_run.values() or [0])
        out.append(Finding(
            "C2", "Long sessions never cleared", "PASS",
            evidence=f"largest session carried ~{fmt_tok(peaks[0])} of history, "
                     f"but never for more than {longest} message(s) in a row - "
                     f"nothing unusual"))
        return out

    # The avoidable share of each session's re-sent history is the part above
    # the threshold. An upper bound: some of that history was genuinely still
    # in use, and settings alone cannot tell which. cost-coach reads the
    # conversation and can.
    rate_usd = blended_input_rate(h)
    avoidable_tok = 0.0
    for k in heavy:
        peak = h.session_peak.get(k, 0) or 1
        above = max(0.0, 1.0 - (HIGH_CONTEXT_TOKENS / peak))
        avoidable_tok += h.session_cache_read.get(k, 0) * above
    avoidable = avoidable_tok * blended_cache_read_rate(h) / 1e6
    worst_run = max(h.session_high_run.get(k, 0) for k in heavy)
    out.append(Finding(
        "C2", "Long sessions never cleared", "FIX",
        monthly_usd=avoidable,
        evidence=f"{len(heavy)} session(s) spent long stretches carrying more "
                 f"than {fmt_tok(HIGH_CONTEXT_TOKENS)} of history - the worst "
                 f"ran {worst_run} messages in a row without a reset (largest "
                 f"peaked at ~{fmt_tok(peaks[0])}); ~{fmt_tok(avoidable_tok)} "
                 "of re-sent history is down to that extra length",
        why="Claude re-sends the whole conversation with every message, so a "
            "message near the end of a long chat can cost five times the same "
            "message at the start. In a session that has been open all day, a "
            "one-line question still carries everything said before it. This "
            "is the biggest single cause of surprise bills in Anthropic's own "
            "cost guidance. Read the figure as an upper bound: it assumes that "
            "extra history was no longer being used, which settings cannot "
            "check.",
        fix="Type /clear when you switch to unrelated work - it starts a fresh "
            "conversation and costs nothing. Use /rename first if you want to "
            "find the old one again with /resume. When you do need the history "
            "carried forward, /compact is worth it despite costing one "
            "expensive request: it re-reads the conversation once, but every "
            "message after it is far cheaper, so it pays for itself within a "
            "few messages. The one time to skip it is when the session is "
            "nearly over.",
        can_fix="NONE", doc=DOCS["context"], kind="bound"))
    return out


def _cache_worst_sessions(h, top=3):
    """Name the sessions with the poorest reuse.

    A single machine-wide percentage cannot tell "my cache keeps breaking"
    apart from "I work in short sessions", and the fix only applies to the
    first. Per-session ratios separate them.
    """
    rows = []
    for key, writes in h.session_writes.items():
        reads = h.session_cache_read.get(key, 0)
        if writes + reads < 50_000:
            continue  # too small to say anything about
        ratio = reads / (reads + writes)
        rows.append((ratio, key, reads, writes))
    if len(rows) < 2:
        return ""
    rows.sort()
    # only name sessions that are actually poor, not the best of a bad list
    worst = [r for r in rows[:top] if r[0] < threshold("cache_hit_ok")]
    if not worst or rows[0][0] > 0.5:
        return " - and no single session stands out, so this looks like short sessions rather than a cache that keeps breaking"
    return (" - worst sessions: "
            + "; ".join(f"{os.path.basename(str(k[1]))[:8]} {r*100:.0f}%"
                        for r, k, _, _ in worst))


def check_cache(h, settings):
    findings = []
    reads = sum(c["cache_read"] for c in h.by_model.values())
    writes = sum(c["w5m"] + c["w1h"] + c["w_unknown"] for c in h.by_model.values())
    fresh = sum(c["in"] for c in h.by_model.values())
    readable = reads + writes + fresh
    if not readable:
        findings.append(Finding("C1", "Paying twice for the same text", "UNKNOWN",
                                evidence="no usage figures in this period"))
        findings.append(Finding("C2", "Long sessions never cleared", "UNKNOWN",
                                evidence="no usage figures in this period"))
        return findings

    rate = reads / readable
    if rate >= threshold("cache_hit_ok"):
        findings.append(Finding(
            "C1", "Paying twice for the same text", "PASS", kind="bound",
            evidence=f"{rate*100:.1f}% of your text was re-used from cache at a tenth of the price"))
        # C2 is about session length, which is independent of cache health -
        # it must not be skipped just because C1 passed.
        findings.extend(_check_long_sessions(h))
        return findings

    # Price it. A cache write bills at 1.25x (5m) or 2x (1h) the input rate;
    # what a read would have cost is 0.1x. The gap is the money genuinely
    # paid a second time. Previously this row showed $0.
    repaid = 0.0
    for model, c in h.by_model.items():
        pr = price_for(model)
        if not pr:
            continue
        crm = cache_read_mult(model)
        repaid += (c["w5m"] * (WRITE_MULT_5M - crm)
                   + (c["w1h"] + c["w_unknown"]) * (WRITE_MULT_1H - crm)
                   ) * pr[0] / 1e6
    findings.append(Finding(
        "C1", "Paying twice for the same text", "FIX",
        monthly_usd=repaid,
        evidence=f"{rate*100:.1f}% cache hits ({fmt_tok(reads)} read vs "
                 f"{fmt_tok(writes)} written, {fmt_tok(fresh)} uncached)"
                 + _cache_worst_sessions(h),
        why="Claude re-uses the conversation so far at about a tenth of the "
            "normal price - that is the cache. When it misses you pay full price "
            "again for text you already paid for, usually because CLAUDE.md or "
            "your settings changed mid-session. One warning: shrinking your "
            "context in a way that breaks the cache can cost more, not less.",
        kind="bound",
        fix="Try not to edit CLAUDE.md or your settings mid-session - finish the "
            "session first. Put the things that do not change at the start of "
            "the conversation. A low figure can also just mean you work in "
            "short sessions, which is fine and costs nothing extra. To see why "
            "a particular session lost its cache, run /usage inside it - it "
            "names the likely cause of the last miss.",
        can_fix="NONE", doc=DOCS["context"]))

    findings.extend(_check_long_sessions(h))
    return findings


def check_habits(h):
    findings = []
    if not h.turns:
        return [Finding("D2", "Lots of screenshots", "UNKNOWN",
                        evidence="no session history to look at yet")]

    total = h.turns

    IMAGE_TOOLS = ("computer", "screenshot", "gif_creator", "upload_image",
                   "session_view", "get_screenshot")
    shots = sum(n for t, n in h.tool_calls.items()
                if any(k in t.lower() for k in IMAGE_TOOLS))
    # A Read of an image file renders it visually - the same cost as a
    # screenshot, and previously invisible to this check.
    shots += h.image_reads
    if shots > max(threshold("screenshot_floor"), total * 0.05):
        # Screenshots are billed as images. ~1.5k tokens is a working figure
        # for a full-window capture; stated so the reader can discount it.
        # width x height / 750. A retina window capture (1512x982) is ~2,600
        # tokens; 1,500 assumed a much smaller image.
        per_image = threshold("image_tokens")
        img_tok = shots * per_image
        # An image is paid once at the fresh-input rate, then rides in the
        # cached prefix for the rest of its session. Charging only the entry
        # cost - as this did - understates the very thing the text warns about.
        turns_per_session = h.turns / max(len(h.sessions), 1)
        carried = max(0.0, turns_per_session / 2)  # average: mid-session
        rate_usd = blended_input_rate(h)
        img_cost = img_tok * rate_usd / 1e6
        img_cost += img_tok * carried * blended_cache_read_rate(h) / 1e6
        findings.append(Finding(
            "D2", "Lots of screenshots", "FIX",
            evidence=f"{shots} screenshot/computer-use calls "
                     f"(~{fmt_tok(img_tok)} image tokens at ~{per_image}/image, "
                     f"each then carried through ~{carried:.0f} later messages)",
            monthly_usd=img_cost,
            why="A screenshot goes into the conversation as a picture. One "
                "picture costs far more than a line of text, and it stays in the "
                "conversation from then on, so you keep paying for it. We "
                "estimate about 2,600 tokens per screenshot, from its pixel "
                "size - a rule of thumb, not a measurement, because the exact "
                "count per image is not recorded.",
            fix="When you only need to know what a page says, ask Claude to read "
                "the page text or the error log instead of taking a picture. Keep "
                "screenshots for when you actually need to see the layout.",
            can_fix="NONE", doc=DOCS["context"], kind="bound"))
    else:
        findings.append(Finding("D2", "Lots of screenshots", "PASS", kind="bound",
                                evidence=f"{shots} screenshots - not enough to matter"))

    if h.dup_reads > max(threshold("dup_read_floor"), total * 0.05):
        # Each repeat read adds another copy to context, then rides along in
        # the cached prefix for the rest of the session.
        dup_tok = h.dup_read_bytes / CHARS_PER_TOKEN if h.dup_read_bytes else 0
        dup_cost = dup_tok * blended_input_rate(h) / 1e6 if dup_tok else 0.0
        findings.append(Finding(
            "D3", "Reading the same file twice", "FIX",
            evidence=f"{h.dup_reads} files were read again after Claude already had them"
                     + (f", adding ~{fmt_tok(dup_tok)} tokens back into the conversation" if dup_tok else ""),
            monthly_usd=dup_cost,
            why="Every time a file is read, a copy of it is added to the "
                "conversation. The first copy does not go away - so now you are "
                "paying for both, on every message that follows. Read this as "
                "an upper bound: re-reading a file that changed in between is "
                "the right thing to do, and we cannot tell those apart. Sizes "
                "are also measured today, not when the read happened.",
            fix="Mostly this is Claude's habit, not yours. If you see it "
                "re-reading a file it already has, say so: 'you already read "
                "that file, use what you have'.",
            can_fix="NONE", doc=DOCS["context"], kind="bound"))
    else:
        findings.append(Finding("D3", "Reading the same file twice", "PASS", kind="bound",
                                evidence=f"{h.dup_reads} files read more than once - not enough to matter"))

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


def calibration_status_payload(saved):
    """What --calibration-status returns. The skill acts on `action`; the
    `how_to_find_it` block is what it should read out when asking, so the
    user is not left guessing which number on which screen."""
    managed = model_pricing_setting()
    if saved and not saved.get("stale"):
        action = "proceed"
    elif saved:
        action = "ask_user_refresh"
    else:
        action = "proceed_managed_rates" if managed else "ask_user"
    out = {
        "calibrated": bool(saved),
        "actual_spend": (saved or {}).get("actual_spend"),
        "spend_since": (saved or {}).get("spend_since"),
        "age_days": (saved or {}).get("age_days"),
        "stale": bool((saved or {}).get("stale")),
        "action": action,
    }
    if managed:
        out["managed_model_pricing"] = managed
    if action in ("ask_user", "ask_user_refresh"):
        out["ask_user_for"] = [
            "amount billed so far this period",
            "date the period started (--spend-since)",
            "time you read it (--spend-asof), so the factor does not drift",
        ]
        out["how_to_find_it"] = {
            "pro_max": "/usage-credits, or claude.ai > Settings > Usage > "
                       "Usage credits",
            "team_enterprise": "claude.ai > Admin settings > Usage, or the "
                               "org spend report from your admin",
            "api_console": "platform.claude.com/usage",
        }
    return out


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


CALIBRATION_HELP = [
    "  Easiest: type  /usage  in Claude Code and paste me the line showing",
    "  what you have spent. I will read the amount off it myself.",
    "",
    "  Or give me the three values directly:",
    "    1. what you have been billed so far this period",
    "    2. the date that period started",
    "    3. the time you read it (so the figure does not drift later)",
    "  Where to look, depending on your plan:",
    "    Pro / Max          type /usage-credits, or claude.ai -> Settings ->",
    "                       Usage -> Usage credits ('$X of $Y spent')",
    "    Team / Enterprise  claude.ai -> Admin settings -> Usage, or ask your",
    "                       admin for the org spend report (per-user, daily)",
    "    API / Console      platform.claude.com/usage",
    "  Then re-run with, for example:",
    "    --actual-spend 120.00 --spend-since 2026-09-01 "
    "--spend-asof 2026-09-19T14:00",
]


def model_pricing_setting():
    """Contracted rates an admin may already have published.

    Better than asking the user for a number off a screen: `modelPricing` is
    a managed setting carrying the organisation's real rates, and Claude Code
    itself uses it for the figures in /usage. If it is present there is
    nothing to calibrate against - we are already on the right prices."""
    for path in ("/Library/Application Support/ClaudeCode/managed-settings.json",
                 "/etc/claude-code/managed-settings.json",
                 os.path.join(CLAUDE_DIR, "managed-settings.json")):
        data = read_json(path)
        if isinstance(data, dict) and data.get("modelPricing"):
            return path
    return None


USAGE_MONEY_RE = re.compile(r"\$\s*([0-9][0-9,]*(?:\.[0-9]{1,2})?)")
USAGE_PAIR_RE = re.compile(
    r"\$\s*([0-9][0-9,]*(?:\.[0-9]{1,2})?)\s*(?:of|/|out of)\s*"
    r"\$\s*([0-9][0-9,]*(?:\.[0-9]{1,2})?)", re.I)
USAGE_TOTAL_RE = re.compile(
    r"total\s+cost\s*:?\s*\$\s*([0-9][0-9,]*(?:\.[0-9]{1,2})?)", re.I)


def _money_float(text):
    try:
        return float(str(text).replace(",", ""))
    except (TypeError, ValueError):
        return None


def parse_usage_paste(text):
    """Pull the spend figure out of a pasted /usage block.

    Asking someone to navigate to an admin page, read three values and note
    the time is three chances to get it wrong. `/usage` already has the
    number on screen, so the cheapest ask is "paste that line".

    Deliberately tolerant: the exact wording of that row is not a documented
    contract and has changed before, so this looks for a "$X of $Y" pair
    first, then a "Total cost:" line, then a lone amount - and always reports
    which shape it matched so the figure can be read back before it is
    trusted. It never guesses between two candidates silently.
    """
    text = (text or "").strip()
    if not text:
        return {"ok": False, "reason": "nothing pasted"}

    pair = USAGE_PAIR_RE.search(text)
    if pair:
        spent, limit = _money_float(pair.group(1)), _money_float(pair.group(2))
        if spent is not None and limit is not None and spent <= limit * 1.5:
            return {"ok": True, "spend": spent, "limit": limit,
                    "matched": "spend-of-limit",
                    "source_line": pair.group(0)}

    total = USAGE_TOTAL_RE.search(text)
    if total:
        spent = _money_float(total.group(1))
        if spent is not None:
            return {"ok": True, "spend": spent, "limit": None,
                    "matched": "total-cost",
                    "note": "This is the CURRENT SESSION's cost, not your "
                            "period spend. Calibrating on one session is "
                            "weaker than on a billing period - prefer the "
                            "usage-credits line if there is one.",
                    "source_line": total.group(0)}

    amounts = [a for a in (_money_float(m) for m in USAGE_MONEY_RE.findall(text))
               if a is not None]
    if len(amounts) == 1:
        return {"ok": True, "spend": amounts[0], "limit": None,
                "matched": "single-amount", "source_line": f"${amounts[0]}"}
    if len(amounts) > 1:
        return {"ok": False, "reason": "found several amounts and cannot tell "
                                       "which is your spend",
                "candidates": amounts[:6],
                "hint": "paste just the usage-credits line, or pass "
                        "--actual-spend yourself"}
    return {"ok": False, "reason": "no dollar amount found in what was pasted"}


def current_period_start():
    """The usage-credits row reports spend for the current calendar month, so
    the period start is derivable and does not need asking for."""
    now = dt.datetime.now()
    return f"{now.year:04d}-{now.month:02d}-01"


def split_totals(findings):
    """Two numbers, never one.

    Adding a counterfactual to arithmetic produced a headline claiming 85% of
    spend was avoidable on the reference machine - because B1 reprices the
    very cache reads C2 calls avoidable, and B3 cuts output tokens B1 has
    already repriced. The measured findings do not overlap each other and are
    summed; the bounds are ranked and shown, never added.
    """
    fixes = [f for f in findings if f.status == "FIX"]
    measured = [f for f in fixes if f.kind == "measured"]
    bounds = sorted([f for f in fixes if f.kind == "bound"],
                    key=lambda f: -f.monthly_usd)
    return {
        "measured_total": sum(f.monthly_usd for f in measured),
        "measured": measured,
        "bounds": bounds,
        "largest_bound": bounds[0] if bounds else None,
    }


def observed_days(h, window_days):
    """How many days of history the figures actually rest on.

    `h.turns`, `h.by_model` and `h.sessions` are all window-scoped, so every
    figure built from them is a total for the period observed - not a month.
    If the machine only has 9 days of transcripts, a 30-day window observed
    9 days, and projecting as though it saw 30 would triple the answer."""
    span = h.span_days or 0
    days = min(window_days, span) if span else window_days
    return max(1.0, float(days))


def normalize_to_month(findings, h, window_days):
    """Scale window totals to a 30-day month.

    Nothing in this script did this: every `monthly_usd` was a total for the
    audit window, printed under a "/mo" label. At the documented `--days 7`
    that understated by roughly 4x."""
    days = observed_days(h, window_days)
    factor = 30.0 / days
    for f in findings:
        f.monthly_usd *= factor
    return {"observed_days": round(days, 1), "factor": round(factor, 3)}


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


def render(findings, h, version, window_days, warnings, cal=None, norm=None):
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
    # The findings are per-month; the spend they are measured against has to
    # be too, or "save $227/mo" sits next to "$200 spent" and the percentage
    # underneath it is nonsense.
    if norm:
        total *= norm["factor"]
    fixes = [f for f in findings if f.status == "FIX"]
    passes = [f for f in findings if f.status == "PASS"]
    sp = split_totals(findings)
    # N/A rows were never judged, so they do not belong in the
    # denominator - they were quietly costing the user 2 points.
    judged = [f for f in findings if f.status in ("PASS", "FIX")]
    score_t = round(len(passes) / max(len(judged), 1) * 100)
    lines.append(f"COST INSPECTOR - claude-code {version or 'unknown'} - "
                 f"{len(h.sessions)} sessions - last {window_days} days")
    if norm and norm["observed_days"] < 29:
        lines.append(f"  (monthly figures projected from {norm['observed_days']} "
                     "days of history - a short window magnifies a busy week)")
    lines.append(BAR)
    if sp["measured_total"]:
        lines.append(f"  WASTE WE MEASURED  {money(sp['measured_total'])}/mo"
                     f"{'':<4}SCORE  {len(passes)}/{len(judged)} ({score_t}%)")
        lines.append("  things nobody is using, charged on every message")
    elif fixes:
        lines.append(f"  NO WASTED SETUP COST{'':<11}SCORE  "
                     f"{len(passes)}/{len(judged)} ({score_t}%)")
    else:
        lines.append(f"  NOTHING TO FIX HERE{'':<12}SCORE  "
                     f"{len(passes)}/{len(judged)} ({score_t}%)")
    if sp["bounds"]:
        lines.append("")
        lines.append("  CHANGING HOW YOU WORK COULD SAVE MORE. Each of these is the")
        lines.append("  MOST it could save, and they overlap each other - so they")
        lines.append("  are listed, never added up:")
        for f in sp["bounds"]:
            lines.append(f"    up to {money(f.monthly_usd):>9}/mo   {f.title}")
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
        managed = model_pricing_setting()
        if managed:
            lines.append("  Your organisation has published its contracted rates in "
                         "managed settings,")
            lines.append(f"  so these may already be close: {_short(managed)}")
        else:
            lines.append("  Tell me what you were actually billed and I will correct "
                         "every figure, and remember it.")
            lines.extend(CALIBRATION_HELP)
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
        # A finding can be a correctness problem rather than a cost one -
        # A3b's ignored AGENTS.md files cost nothing precisely because they
        # are ignored. Labelling those "cost not quantified" under a "Why
        # this costs" heading says the opposite of what they mean.
        correctness = not impact
        tag = "  ".join(impact) or "costs nothing - but it is not working"
        if f.kind == "bound" and f.monthly_usd:
            tag = "  ".join(impact[:-1] + [f"up to {money(f.monthly_usd)}/mo"])
        lines.append("")
        lines.append(f"FIX  {f.title}{'':<4}{tag}")
        lines.append(f"     Who fixes it: {FIXER_LABEL.get(f.can_fix, f.can_fix)}")
        lines.append(f"     Found: {f.evidence}")
        lines.append("")
        label = "Why it matters: " if correctness else "Why this costs:"
        for i, chunk in enumerate(_wrap(f.why, 57)):
            lines.append(f"     {label} {chunk}" if i == 0
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


def render_markdown(findings, h, version, window_days, warnings, cal, ref, norm=None):
    """Markdown twin of render(). The terminal wants fixed-width columns; a
    saved .md file wants real markdown, not ASCII art with a renamed suffix."""
    total = sum(v for m, c in h.by_model.items()
                for v in [model_cost(m, c)] if v is not None)
    if cal:
        total *= cal["factor"]
    if norm:
        total *= norm["factor"]
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
    sp = split_totals(findings)
    if sp["measured_total"]:
        pct = (sp["measured_total"] / total * 100) if total else 0
        out.append(f"## {money(sp['measured_total'])} a month is going on "
                   "things nobody is using")
        out.append("")
        out.append(f"That is {pct:.0f}% of your {money(total)} "
                   f"{spend_label.lower()}, and it is charged on every message "
                   "whatever you do. **{} of {} checks passed.**".format(
                       len(passes), len(findings)))
    elif fixes:
        out.append("## Your setup itself is not wasting money")
        out.append("")
        out.append(f"**{len(passes)} of {len(findings)} checks passed.** What is "
                   "left is about how the tool is used, below.")
    else:
        out.append("## Nothing worth changing")
        out.append("")
        out.append(f"**{len(passes)} of {len(findings)} checks passed**, and the "
                   "rest have no measurable cost.")
    out.append("")

    if sp["bounds"]:
        out.append("### Changing how you work could save more")
        out.append("")
        out.append("Each figure below is **the most it could save**, not a "
                   "forecast — it assumes a cheaper model would have finished "
                   "the same work, or that you would have cleared the session. "
                   "They also overlap each other, so they are listed separately "
                   "and **not added together or added to the figure above**.")
        out.append("")
        out.append("| If you changed this | At most |")
        out.append("|---|---|")
        for f in sp["bounds"]:
            out.append(f"| {f.title} | up to {money(f.monthly_usd)}/mo |")
        out.append("")

    if fixes_sorted:
        top = fixes_sorted[0]
        kind_note = (" — the most it could save, not a forecast"
                     if top.kind == "bound" else "")
        out.append(f"**Start here:** {top.title} — {money(top.monthly_usd)} a "
                   f"month{kind_note}.")
        out.append("")
        # "up to" is not decoration: without it the two kinds of number look
        # alike in the one place most people copy out of.
        out.append("| What we found | Saves/month | Who does it |")
        out.append("|---|---|---|")
        for f in fixes_sorted:
            amt = (f"up to {money(f.monthly_usd)}" if f.kind == "bound"
                   else money(f.monthly_usd))
            out.append(f"| {f.title} | {amt} "
                       f"| {FIXER_LABEL.get(f.can_fix, f.can_fix)} |")
        out.append("")
        out.append("*\"up to\" marks a figure that depends on working "
                   "differently — those overlap each other and must not be "
                   "added up.*")
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
               if f.id in ("D2", "D3") and f.status == "FIX"]
    peak = _forecast_coach_cost(h)
    if not signals:
        return {"recommended": False,
                "reason": "session patterns look efficient",
                "signals": [], "forecast_usd": peak}
    names = {"D2": "heavy screenshot use",
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
        # Two totals, deliberately. cost-coach must not re-add them: the
        # bounds overlap each other and the measured set.
        "measured_total_usd": round(
            sum(f.monthly_usd for f in findings
                if f.status == "FIX" and f.kind == "measured"), 2),
        "bounds_usd": {f.id: round(f.monthly_usd, 2) for f in findings
                       if f.status == "FIX" and f.kind == "bound"},
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
    ap.add_argument("--usage-paste", metavar="TEXT",
                    help="calibrate from a pasted /usage block ('-' reads "
                         "stdin). Derives the billing period from the current "
                         "month and the reading time from now, so the user "
                         "only has to paste one line.")
    ap.add_argument("--calibration-status", action="store_true",
                    help="print whether a calibration is saved, and exit. Cheap "
                         "(no transcript scan) - call this first so you can ask "
                         "the user for their real spend before running")
    ap.add_argument("--install-hook", action="store_true",
                    help="install the first-prompt model/effort nudge hook")
    ap.add_argument("--remove-hook", action="store_true",
                    help="remove the first-prompt nudge hook")
    args = ap.parse_args()

    if args.usage_paste:
        text = (sys.stdin.read() if args.usage_paste == "-"
                else args.usage_paste)
        parsed = parse_usage_paste(text)
        if parsed.get("ok"):
            since = args.spend_since or current_period_start()
            asof = args.spend_asof or dt.datetime.now().strftime("%Y-%m-%dT%H:%M")
            save_calibration_config(parsed["spend"], since, asof)
            parsed.update({"saved": True, "spend_since": since,
                           "spend_asof": asof,
                           "read_back": f"Calibrating against "
                                        f"{money(parsed['spend'])} spent since "
                                        f"{since}. Tell me if that is not the "
                                        "right number."})
        print(json.dumps(parsed, indent=2))
        return 0 if parsed.get("ok") else 1

    if args.calibration_status:
        payload = calibration_status_payload(load_calibration_config())
        if payload["action"].startswith("ask_user"):
            payload["prompt"] = (
                "Ask the user for the amount billed so far this period, the date "
                "the period started, and the time they read it, then re-run with "
                "--actual-spend, --spend-since and --spend-asof. Read out the "
                "how_to_find_it entry that matches their plan - do not just say "
                "'your usage panel'. Without this every dollar figure is a "
                "list-price estimate, typically ~1.8x too high.")
        print(json.dumps(payload, indent=2))
        return 0
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
    skills = scan_skills(cwd)
    md = scan_claude_md(cwd)
    servers = mcp_servers(cwd)
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
    findings += check_claude_md(md, h, wmult, elsewhere, cwd, settings)
    findings += check_skills(skills, h, args.days, wmult,
                             settings.get("enabledPlugins"))
    findings += check_memory(cwd, h, wmult)
    findings += check_mcp(servers, h, args.days, wmult)
    findings += check_models(h, settings, sources)
    findings += check_fast_mode(h, wmult)
    findings += check_cache(h, settings)
    findings += check_habits(h)

    # Order is irrelevant (both are scalar multipliers) but normalisation
    # must not touch the calibration estimate, which is compared against a
    # real bill for a real period.
    norm = normalize_to_month(findings, h, args.days)
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

    report = render(findings, h, version, args.days, warnings, cal, norm)
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
        md = render_markdown(findings, h, version, args.days, warnings, cal, ref, norm)
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
