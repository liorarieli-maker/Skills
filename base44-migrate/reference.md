# Base44 → Vercel + Supabase Translation Patterns

## Entity CRUD Operations

| Base44 | Supabase (frontend, anon client) |
|--------|----------------------------------|
| `base44.entities.X.list()` | `supabase.from('x').select('*')` |
| `base44.entities.X.list({ field: value })` | `supabase.from('x').select('*').eq('field', value)` |
| `base44.entities.X.get(id)` | `supabase.from('x').select('*').eq('id', id).single()` |
| `base44.entities.X.create(data)` | `supabase.from('x').insert(data).select().single()` |
| `base44.entities.X.update(id, data)` | `supabase.from('x').update(data).eq('id', id).select().single()` |
| `base44.entities.X.delete(id)` | `supabase.from('x').delete().eq('id', id)` |
| `base44.entities.X.bulkCreate(items)` | `supabase.from('x').insert(items).select()` |

## Auth Operations

| Base44 | Supabase |
|--------|----------|
| `base44.auth.me()` | `supabase.auth.getUser()` → use `.data.user` |
| `base44.auth.logout()` | `supabase.auth.signOut()` |
| `base44.auth.redirectToLogin()` | `supabase.auth.signInWithOAuth({ provider: 'google' })` |
| `base44.auth.isAuthenticated()` | check `supabase.auth.getSession()` → `.data.session !== null` |
| Auth user ID | `session.user.id` or `user.id` |
| Auth user email | `session.user.email` or `user.email` |

## Function Invocation

| Base44 | Vercel + Supabase |
|--------|-------------------|
| `base44.functions.invoke('name', payload)` | `fetch('/api/name', { method: 'POST', headers: { Authorization: 'Bearer ' + token }, body: JSON.stringify(payload) })` |
| Supabase Edge Function | `supabase.functions.invoke('name', { body: payload })` |

## LLM / AI

| Base44 | OpenAI SDK |
|--------|------------|
| `Core.InvokeLLM({ prompt, response_json_schema })` | `openai.chat.completions.create({ model: 'gpt-4o', messages: [{ role: 'user', content: prompt }], response_format: { type: 'json_object' } })` |
| `Core.InvokeLLM({ messages })` | `openai.chat.completions.create({ model: 'gpt-4o', messages })` |

## Backend Function Patterns

### Vercel Function (Node.js, fast operations)
```ts
// api/my-function.ts
import { createClient } from '@supabase/supabase-js'

export default async function handler(req: any, res: any) {
  if (req.method !== 'POST') return res.status(405).end()

  const token = req.headers.authorization?.replace('Bearer ', '')
  if (!token) return res.status(401).json({ error: 'Unauthorized' })

  const supabase = createClient(
    process.env.SUPABASE_URL!,
    process.env.SUPABASE_SERVICE_ROLE_KEY!
  )

  const { data: { user } } = await supabase.auth.getUser(token)
  if (!user) return res.status(401).json({ error: 'Invalid token' })

  const body = req.body
  // ... your logic ...
  res.status(200).json({ result })
}
```

### Supabase Edge Function (Deno, long-running)
```ts
// supabase/functions/my-function/index.ts
import { serve } from 'https://deno.land/std@0.168.0/http/server.ts'
import { createClient } from 'https://esm.sh/@supabase/supabase-js@2'

serve(async (req) => {
  const token = req.headers.get('Authorization')?.replace('Bearer ', '')
  if (!token) return new Response(JSON.stringify({ error: 'Unauthorized' }), { status: 401 })

  const supabase = createClient(
    Deno.env.get('SUPABASE_URL')!,
    Deno.env.get('SUPABASE_SERVICE_ROLE_KEY')!
  )

  const { data: { user } } = await supabase.auth.getUser(token)
  if (!user) return new Response(JSON.stringify({ error: 'Invalid token' }), { status: 401 })

  const body = await req.json()
  // ... your logic ...
  return new Response(JSON.stringify({ result }), {
    headers: { 'Content-Type': 'application/json' }
  })
})
```

## Service Role (Admin) Operations

| Base44 | Supabase |
|--------|----------|
| `base44.asServiceRole.entities.X.update(id, data)` | Use service role client: `supabaseAdmin.from('x').update(data).eq('id', id)` |
| `base44.asServiceRole.entities.X.create(data)` | `supabaseAdmin.from('x').insert(data).select().single()` |
| `base44.asServiceRole.entities.X.list()` | `supabaseAdmin.from('x').select('*')` |

The service role client bypasses RLS. Only use it in backend functions (api/*.ts or supabase/functions/), never in frontend code.

## Error Handling Pattern

```ts
// Supabase returns { data, error }
const { data, error } = await supabase.from('x').select('*')
if (error) throw new Error(error.message)
return data
```

## Filtering Patterns

| Base44 | Supabase |
|--------|----------|
| `.list({ status: 'active' })` | `.select('*').eq('status', 'active')` |
| `.list({ user_id: userId })` | `.select('*').eq('user_id', userId)` |
| `.list({ id: { in: ids } })` | `.select('*').in('id', ids)` |
| Order by date | `.select('*').order('created_date', { ascending: false })` |
| Limit results | `.select('*').limit(10)` |

## invokeApi Helper (frontend)

```js
// src/api/supabaseClient.js
export async function invokeApi(name, payload = {}) {
  const { data: { session } } = await supabase.auth.getSession()
  const token = session?.access_token
  if (!token) throw new Error('Not authenticated')

  const res = await fetch(`/api/${name}`, {
    method: 'POST',
    headers: {
      'Content-Type': 'application/json',
      'Authorization': `Bearer ${token}`
    },
    body: JSON.stringify(payload)
  })

  if (!res.ok) {
    const err = await res.json().catch(() => ({ error: res.statusText }))
    throw new Error(err.error || 'API error')
  }

  return res.json()
}
```

## Common Base44 Idioms and Their Equivalents

### Polling for async results
Base44 sometimes fires a function and polls for the result. In Supabase:
- Fire the function via `invokeApi` (fire-and-forget OK)
- Poll by calling the entity list/get repeatedly, or use Supabase Realtime subscriptions

### File/Image Upload
Base44 had built-in file hosting. In Supabase:
- Use Supabase Storage: `supabase.storage.from('bucket').upload(path, file)`
- Get public URL: `supabase.storage.from('bucket').getPublicUrl(path)`

### `created_by` field
In Supabase, every table should have a `created_by UUID REFERENCES auth.users(id)` column.
RLS policies use `auth.uid() = created_by` to restrict access.

### Date fields
Base44 uses `created_date`. Keep this name for compatibility. Set it as `TIMESTAMPTZ DEFAULT NOW()` in Postgres.

## Deno → Node.js Function Translation

| Deno (Base44 Edge Functions) | Node.js (Vercel) |
|------------------------------|------------------|
| `serve((req) => ...)` | `export default function handler(req, res)` |
| `await req.json()` | `req.body` (auto-parsed by Vercel) |
| `req.headers.get('Authorization')` | `req.headers.authorization` |
| `new Response(JSON.stringify(data))` | `res.json(data)` |
| `Deno.env.get('VAR')` | `process.env.VAR` |
| `import { X } from 'npm:package'` | `import { X } from 'package'` |
| `import { X } from 'https://esm.sh/package'` | `import { X } from 'package'` |
