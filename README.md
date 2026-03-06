# base44-migrate

An [Agent Skill](https://agentskills.io) that migrates a [Base44](https://base44.com) app to [Vercel](https://vercel.com) + [Supabase](https://supabase.com).

## What it does

Scans your Base44 project, generates a customized migration plan, and implements all the changes — rewriting auth, entities, backend functions, DB schema, and deployment config.

## Compatible tools

Works in any [Agent Skills-compatible](https://agentskills.io) tool: Claude Code, Cursor, GitHub Copilot, VS Code, OpenAI Codex, and more.

## Install

**Claude Code:**
```bash
git clone https://github.com/liorarieli-maker/Skills.git /tmp/Skills
cp -r /tmp/Skills/base44-migrate ~/.claude/skills/
```

**Cursor:**
```bash
git clone https://github.com/liorarieli-maker/Skills.git /tmp/Skills
cp -r /tmp/Skills/base44-migrate ~/.cursor/skills/
```

## Usage

In your agent's chat, type:
```
/base44-migrate
```

## What gets migrated

| From (Base44) | To |
|---|---|
| `base44.auth.*` | Supabase Auth + Google OAuth |
| `base44.entities.*` | Supabase PostgreSQL + RLS |
| `/functions/*.ts` | Vercel serverless functions or Supabase Edge Functions |
| Base44 hosting | Vercel CDN |

See [reference.md](./reference.md) for the full translation table.
