---
name: base44-migrate
description: Migrate a Base44 app to Vercel (frontend + serverless functions) and Supabase (PostgreSQL + Auth). Scans the repo, generates a customized migration plan, and implements all changes.
argument-hint: "[path-to-extracted-base44-zip-or-leave-blank-to-use-current-dir]"
---

# Base44 → Vercel + Supabase Migration Skill

You are guiding the user through a complete migration of their Base44 app to Vercel + Supabase. Follow every phase in order. Do not skip phases. Do not do phases out of order. Stop and wait for user input at every checkpoint.

---

## PHASE 0 — Get the Code

First, explain to the user:

> "We're going to migrate your Base44 app to run on Vercel (hosting + serverless functions) and Supabase (database + auth). This creates a brand new codebase — your Base44 app stays untouched.
>
> **Step 1:** In Base44, open your app → click the **more actions menu** (upper-right corner) → **Export project as ZIP**.
> Extract the ZIP to a folder on your Mac (e.g. `~/Downloads/my-app`).
> Then paste the folder path here."

Wait for the user to paste the folder path.

---

## PHASE 0b — Scan the Code

Once you have the folder path:

1. Read `package.json` → confirm `@base44/sdk` or `@base44/vite-plugin` is present.
2. Recursively search all `.js`, `.jsx`, `.ts`, `.tsx` files for:
   - `base44.entities.` → **flag: needs DB** (note which entity names appear)
   - `base44.auth.` → **flag: needs Auth — but read the actual usage context before flagging**
   - `base44.functions.invoke(` → **flag: needs backend functions** (note which function names)
   - `Core.InvokeLLM(` or `openai` → **flag: needs OpenAI**
   - `paypal` or `PayPal` → **flag: needs PayPal**
   - Any other third-party API calls
3. **For any `base44.auth.` usage found**, read the actual files and check:
   - Is the app set to "Public (no login)" in Base44? (Check if `AuthContext` only checks auth when a token is already present, and the app renders without requiring login.)
   - Is `base44.auth.me()` only used to detect an admin/owner for dev-tool UI (e.g. showing an "Admin Note" on 404 pages)?
   - If YES to either: **do NOT flag as needing Auth**. The auth usage is Base44 internal tooling, not real user auth. Simply remove it during migration.
   - Only flag **needs Auth** if the app actually gates content or routes behind a login wall for end users.
4. Read all files in `/functions/` if present → note what each function does, estimate if any run >10 seconds.

Based on findings, build the prerequisites list. Only include what's actually needed.

**Always needed:**
- GitHub account
- `gh` CLI
- Vercel account
- Vercel CLI (`vercel`)

**Only if DB or real user Auth detected:**
- Supabase account
- Supabase CLI (`supabase`)

Present the prerequisites list to the user:
> "Based on your app, here's what we need to set up. Let's go through them one by one."

---

## PHASE 0c — Prerequisites Setup

Go through each prerequisite one by one:

### `gh` CLI + GitHub Account
Run: `gh --version`
- If missing: print `brew install gh` (macOS) or https://cli.github.com for other OS. Wait, then re-check.

Run: `gh auth status`
- If already authenticated: GitHub account is confirmed — skip asking about it.
- If not authenticated: Ask "Do you have a GitHub account? If not, go to github.com/signup — it's free. Type 'done' when ready." Then run `gh auth login` and guide through the interactive flow.

### Vercel CLI + Vercel Account
Run: `vercel --version`
- If missing: run `npm install -g vercel`. Wait, then re-check.

Run: `vercel whoami`
- If already authenticated: Vercel account is confirmed — skip asking about it.
- If not authenticated: Ask "Do you have a Vercel account? If not, go to vercel.com and sign up with GitHub — free tier is enough. Type 'done' when ready." Then run `vercel login` and guide through.

### Supabase CLI + Supabase Account *(only if DB or Auth detected)*
Run: `supabase --version`
- If missing: run `brew install supabase/tap/supabase`. Wait, then re-check.

Run: `supabase projects list`
- If already authenticated: Supabase account is confirmed — skip asking about it.
- If not authenticated: Ask "Do you have a Supabase account? If not, go to supabase.com and sign up — free tier is enough. Type 'done' when ready." Then run `supabase login` and guide through.

Once all confirmed: "Great! All prerequisites are set up. Let's start the migration."

---

## PHASE 1 — Collect Info

Ask the following questions one at a time:

1. "What is your GitHub username?"
2. "What should the new repo be called? (e.g. `my-app-vercel`)"
3. "Should the repo be **public** or **private**?"

Confirm back:
> "Got it. I'll create `github.com/{username}/{repo-name}` as a {public/private} repo and start the migration. Ready?"

Wait for confirmation.

---

## PHASE 2 — Generate Migration Plan

Based on the scan from Phase 0b, generate and display a migration plan:

```
MIGRATION PLAN
==============

New repo: github.com/{username}/{repo-name}

DB Tables needed:
  - {entity1} (columns: id, created_date, {detected fields})
  - {entity2} (...)

API Routes needed:
  - /api/{function1}  [Vercel — fast]
  - /api/{function2}  [Vercel — fast]
  - (or Supabase Edge Function if estimated >10s)

Code changes:
  - package.json: remove @base44/sdk, add @supabase/supabase-js
  - vite.config.js: remove Base44 plugin
  - src/lib/AuthContext.jsx: rewrite for Supabase Auth
  - src/api/supabaseClient.js: NEW — Supabase client + invokeApi helper
  - src/api/entities.js: NEW — entity CRUD helpers
  - supabase/schema.sql: NEW — DB schema
  - api/_lib.ts: NEW — shared backend auth helper
  - api/{function}.ts: NEW — each backend function
  - vercel.json: NEW — Vercel config
  - .env.example: NEW — required env vars
```

Ask: "Does this plan look right? Type 'yes' to proceed or let me know what to change."

---

## PHASE 3 — Implement

Implement everything automatically in this order:

### Step 1: Create GitHub repo and push code
```bash
cd {folder-path}
gh repo create {username}/{repo-name} --{public|private} --source=. --remote=origin --push
```

### Step 2: Update package.json
Remove: `@base44/sdk`, `@base44/vite-plugin`
Add: `@supabase/supabase-js`

Run: `npm install` (or `bun install` / `yarn` based on what's in the project)

### Step 3: Update vite.config.js
Remove the Base44 plugin import and its usage. Keep everything else.

### Step 4: Create src/api/supabaseClient.js
Use the template at `templates/supabaseClient.js`. Customize `invokeApi` based on detected function names.

### Step 5: Rewrite src/lib/AuthContext.jsx
Use the template at `templates/AuthContext.jsx`.

### Step 6: Create src/api/entities.js
Use the template at `templates/entities.js`. Customize with the actual entity names detected. For each entity `X` detected:
- Add `export const X = { list, create, update, delete }` functions
- Each function calls Supabase from the appropriate table name (lowercase snake_case of X)

### Step 7: Create supabase/schema.sql
Use the template at `templates/schema.sql`. Add a `CREATE TABLE` for each detected entity with:
- `id UUID DEFAULT uuid_generate_v4() PRIMARY KEY`
- `created_by UUID REFERENCES auth.users(id)`
- `created_date TIMESTAMPTZ DEFAULT NOW()`
- Any additional columns you can infer from usage in the source code (e.g., if code reads `.name`, `.status`, `.title`, add those as `TEXT`)
- `updated_date TIMESTAMPTZ DEFAULT NOW()`
- RLS policies: users can only read/write their own rows

### Step 8: Create api/_lib.ts
Use the template at `templates/_lib.ts`.

### Step 9: Rewrite each function
For each function in `/functions/`:
- Read the original function
- If it's fast (<10s): create `api/{name}.ts` as a Vercel function
  - Use the handler pattern: `export default async function handler(req, res)`
  - Translate Deno/Base44 patterns to Node.js
  - Replace `base44.asServiceRole.entities.X.*` with Supabase service role client calls
  - Replace `Core.InvokeLLM({...})` with `openai.chat.completions.create({...})`
- If it's slow (>10s): create `supabase/functions/{name}/index.ts` as an Edge Function

### Step 10: Create vercel.json
Use the template at `templates/vercel.json`.

### Step 11: Create .env.example
Based on what's needed:
```
# Always
VITE_SUPABASE_URL=
VITE_SUPABASE_ANON_KEY=

# If backend functions exist
SUPABASE_URL=
SUPABASE_SERVICE_ROLE_KEY=

# If OpenAI detected
OPENAI_API_KEY=

# If PayPal detected
PAYPAL_CLIENT_ID=
PAYPAL_CLIENT_SECRET=
```

### Step 12: Commit and push all changes
```bash
git add -A
git commit -m "Migrate from Base44 to Vercel + Supabase"
git push
```

---

## PHASE 4 — Supabase Project Setup *(only if DB or Auth detected)*

Guide the user through creating a Supabase project:

1. "Go to supabase.com → New Project. Choose a name and a strong database password (save it somewhere). Select the region closest to you. Click 'Create new project'. Type 'done' when it's ready (takes ~1 minute)."

2. "Now go to: Project Settings → API. Copy these three values and paste them here one by one:
   - **Project URL** (looks like `https://xxxx.supabase.co`)
   - **anon public** key (long JWT string under 'Project API keys')
   - **service_role** key (long JWT string — keep this secret!)"

3. Run the schema: `supabase db push --db-url postgresql://postgres:{password}@db.{project-ref}.supabase.co:5432/postgres < supabase/schema.sql`
   (Or guide them to paste the schema.sql contents into the Supabase SQL editor and run it.)

4. *(Only if Auth detected)* Guide Google OAuth setup:
   - Supabase dashboard → Authentication → Providers → Google → Enable
   - "Now go to console.cloud.google.com → New Project → APIs & Services → OAuth consent screen → External → fill in app name + email → Save."
   - "Then: Credentials → Create Credentials → OAuth 2.0 Client ID → Web Application"
   - "Add Authorized redirect URI: `https://{your-supabase-project}.supabase.co/auth/v1/callback`"
   - "Copy the Client ID and Client Secret → paste them back in Supabase under the Google provider settings."

---

## PHASE 5 — Collect Secrets & Deploy

### Collect all needed secrets

For each detected integration, ask one at a time:

**If OpenAI detected:**
> "Your app uses AI. You need an OpenAI API key. Go to platform.openai.com → API Keys → Create new secret key → paste it here."

**If PayPal detected:**
> "Your app has PayPal payments. Go to developer.paypal.com → My Apps & Credentials → your app → copy the Client ID and Secret → paste them here."

**Any other detected integrations:** ask similarly with plain-English instructions.

### Set env vars in Vercel

For each secret collected, run:
```bash
vercel env add {VAR_NAME} production
```
(Vercel CLI will prompt for the value interactively.)

Also set:
- `VITE_SUPABASE_URL`
- `VITE_SUPABASE_ANON_KEY`
- `SUPABASE_URL` *(if backend functions)*
- `SUPABASE_SERVICE_ROLE_KEY` *(if backend functions)*

### Deploy frontend
```bash
vercel --prod
```

### Deploy Edge Functions *(only if any Edge Functions were created)*
```bash
supabase functions deploy {function-name} --project-ref {project-ref}
```

---

## PHASE 6 — Custom Domain *(optional)*

Ask: "Do you have a custom domain you'd like to use? (e.g. myapp.com) Type 'skip' to skip."

If yes:
1. "Go to your Vercel project dashboard → Settings → Domains → Add domain → enter `{domain}`."
2. "Vercel will show you DNS records to add. Go to your domain registrar (GoDaddy, Namecheap, Cloudflare, etc.) and add:
   - An **A record** pointing `@` to Vercel's IP: `76.76.21.21`
   - A **CNAME record** pointing `www` to `cname.vercel-dns.com`"
3. "DNS changes take up to 48 hours to propagate, but usually just a few minutes."

---

## PHASE 7 — Done

Print a summary:

```
MIGRATION COMPLETE
==================

Live URL: {vercel-url}
GitHub:   https://github.com/{username}/{repo-name}
Supabase: https://supabase.com/dashboard/project/{project-ref}

Your Base44 app is untouched. This is a completely new deployment.

Next steps:
- Test all features end-to-end
- Invite collaborators to GitHub if needed
- Monitor Vercel and Supabase dashboards for usage
```

---

## Important Notes

- **Never break the user's existing Base44 app.** Always work in a fresh repo.
- **Detect, don't assume.** Only add what the scan found. Don't add OpenAI if the app doesn't use it.
- **Be patient with the user.** These are non-trivial infrastructure steps. Give clear, numbered instructions.
- **Supabase RLS is critical.** Every table must have Row Level Security enabled so users can only access their own data.
- **Long-running functions** (>10s) must go to Supabase Edge Functions, not Vercel (which has a 10s timeout on hobby plan, 300s on Pro).
- Refer to `reference.md` for all Base44 → Supabase translation patterns.
- Refer to `templates/` for all file templates.
