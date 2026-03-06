/**
 * Shared backend auth helper for Vercel serverless functions.
 * Import this in every api/*.ts file.
 *
 * Usage:
 *   import { supabaseAdmin, verifyAuth } from './_lib'
 *
 *   export default async function handler(req, res) {
 *     const user = await verifyAuth(req, res)
 *     if (!user) return  // verifyAuth already sent 401
 *
 *     // use supabaseAdmin for DB ops that bypass RLS
 *     const { data } = await supabaseAdmin.from('my_table').select('*')
 *     res.json(data)
 *   }
 */

import { createClient } from '@supabase/supabase-js'

if (!process.env.SUPABASE_URL) throw new Error('Missing SUPABASE_URL')
if (!process.env.SUPABASE_SERVICE_ROLE_KEY) throw new Error('Missing SUPABASE_SERVICE_ROLE_KEY')

/**
 * Supabase admin client — uses service role key, bypasses RLS.
 * NEVER expose this client to the frontend.
 */
export const supabaseAdmin = createClient(
  process.env.SUPABASE_URL,
  process.env.SUPABASE_SERVICE_ROLE_KEY
)

/**
 * Verify the incoming request has a valid Supabase Bearer token.
 * Sends 401 automatically if invalid.
 * Returns the authenticated user, or null if unauthorized.
 */
export async function verifyAuth(req: any, res: any) {
  const token = req.headers.authorization?.replace('Bearer ', '')

  if (!token) {
    res.status(401).json({ error: 'Missing authorization token' })
    return null
  }

  const { data: { user }, error } = await supabaseAdmin.auth.getUser(token)

  if (error || !user) {
    res.status(401).json({ error: 'Invalid or expired token' })
    return null
  }

  return user
}
