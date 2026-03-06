-- Supabase Schema Template
-- Run this in your Supabase project: SQL Editor → New Query → paste + Run
-- Or via CLI: supabase db push

-- Enable UUID generation
CREATE EXTENSION IF NOT EXISTS "uuid-ossp";

-- ─── ENTITY_NAME ──────────────────────────────────────────────────────────────
-- REPLACE with your actual entity. Add one block per entity detected in your app.

CREATE TABLE IF NOT EXISTS entity_name (
  id           UUID DEFAULT uuid_generate_v4() PRIMARY KEY,
  created_by   UUID REFERENCES auth.users(id) ON DELETE CASCADE,
  created_date TIMESTAMPTZ DEFAULT NOW(),
  updated_date TIMESTAMPTZ DEFAULT NOW(),

  -- ADD YOUR ENTITY-SPECIFIC COLUMNS HERE
  -- Examples:
  -- name         TEXT,
  -- status       TEXT DEFAULT 'active',
  -- description  TEXT,
  -- metadata     JSONB DEFAULT '{}'
);

-- Enable Row Level Security
ALTER TABLE entity_name ENABLE ROW LEVEL SECURITY;

-- RLS Policies: users can only access their own rows
CREATE POLICY "Users can read own rows"
  ON entity_name FOR SELECT
  USING (auth.uid() = created_by);

CREATE POLICY "Users can insert own rows"
  ON entity_name FOR INSERT
  WITH CHECK (auth.uid() = created_by);

CREATE POLICY "Users can update own rows"
  ON entity_name FOR UPDATE
  USING (auth.uid() = created_by)
  WITH CHECK (auth.uid() = created_by);

CREATE POLICY "Users can delete own rows"
  ON entity_name FOR DELETE
  USING (auth.uid() = created_by);

-- Auto-update updated_date trigger
CREATE OR REPLACE FUNCTION update_updated_date()
RETURNS TRIGGER AS $$
BEGIN
  NEW.updated_date = NOW();
  RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER entity_name_updated_date
  BEFORE UPDATE ON entity_name
  FOR EACH ROW EXECUTE FUNCTION update_updated_date();


-- ─── ADD MORE TABLES BELOW ────────────────────────────────────────────────────
-- Copy the block above and replace entity_name for each additional entity.
--
-- Example for a "research" table:
--
-- CREATE TABLE IF NOT EXISTS research (
--   id              UUID DEFAULT uuid_generate_v4() PRIMARY KEY,
--   created_by      UUID REFERENCES auth.users(id) ON DELETE CASCADE,
--   created_date    TIMESTAMPTZ DEFAULT NOW(),
--   updated_date    TIMESTAMPTZ DEFAULT NOW(),
--   position_title  TEXT,
--   company_name    TEXT,
--   status          TEXT DEFAULT 'pending',
--   result          JSONB
-- );
-- ALTER TABLE research ENABLE ROW LEVEL SECURITY;
-- CREATE POLICY "Users can read own research" ON research FOR SELECT USING (auth.uid() = created_by);
-- CREATE POLICY "Users can insert own research" ON research FOR INSERT WITH CHECK (auth.uid() = created_by);
-- CREATE POLICY "Users can update own research" ON research FOR UPDATE USING (auth.uid() = created_by);
-- CREATE POLICY "Users can delete own research" ON research FOR DELETE USING (auth.uid() = created_by);
