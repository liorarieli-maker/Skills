/**
 * Entity helpers — Supabase CRUD wrappers
 *
 * TEMPLATE: Replace each ENTITY_NAME / table_name with your actual entity names.
 * Add one section per entity detected in your Base44 app.
 *
 * Naming convention:
 *   Base44 entity: MyEntity  →  Supabase table: my_entity  →  JS export: MyEntity
 *
 * Each entity exposes: list, get, create, update, delete (and bulkCreate if needed)
 */

import { supabase } from './supabaseClient'

// ─── Helper ──────────────────────────────────────────────────────────────────

function handleError({ data, error }) {
  if (error) throw new Error(error.message)
  return data
}

// ─── ENTITY_NAME (table: entity_name) ────────────────────────────────────────
// REPLACE THIS SECTION WITH YOUR ACTUAL ENTITIES

export const EntityName = {
  async list(filters = {}) {
    let query = supabase.from('entity_name').select('*')
    for (const [key, value] of Object.entries(filters)) {
      query = query.eq(key, value)
    }
    query = query.order('created_date', { ascending: false })
    return handleError(await query)
  },

  async get(id) {
    return handleError(
      await supabase.from('entity_name').select('*').eq('id', id).single()
    )
  },

  async create(data) {
    return handleError(
      await supabase.from('entity_name').insert(data).select().single()
    )
  },

  async update(id, data) {
    return handleError(
      await supabase.from('entity_name').update(data).eq('id', id).select().single()
    )
  },

  async delete(id) {
    return handleError(
      await supabase.from('entity_name').delete().eq('id', id)
    )
  },

  async bulkCreate(items) {
    return handleError(
      await supabase.from('entity_name').insert(items).select()
    )
  }
}

// ─── ADD MORE ENTITIES BELOW ──────────────────────────────────────────────────
// Copy the block above and replace entity_name / EntityName for each one.
//
// Example for a "Research" entity:
//
// export const Research = {
//   async list(filters = {}) { ... supabase.from('research') ... },
//   async get(id) { ... },
//   async create(data) { ... },
//   async update(id, data) { ... },
//   async delete(id) { ... }
// }
