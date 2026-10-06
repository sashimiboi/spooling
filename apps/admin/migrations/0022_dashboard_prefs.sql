-- Add dashboard_prefs column to user_settings (previously added at runtime in the API route).
ALTER TABLE user_settings ADD COLUMN IF NOT EXISTS dashboard_prefs JSONB;
