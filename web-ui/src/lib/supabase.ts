/**
 * Supabase client singleton for the Obscura admin portal.
 *
 * Configuration comes from Vite env vars:
 *   - VITE_SUPABASE_URL       — https://<project>.supabase.co
 *   - VITE_SUPABASE_ANON_KEY  — anon public key from the Supabase dashboard
 *
 * When either var is missing the client is `null` and the UI falls back to
 * API-key-only auth (so local dev keeps working before a Supabase project
 * has been provisioned).
 */

import { createClient, type SupabaseClient } from '@supabase/supabase-js';

const SUPABASE_URL = import.meta.env.VITE_SUPABASE_URL as string | undefined;
const SUPABASE_ANON_KEY = import.meta.env.VITE_SUPABASE_ANON_KEY as
  | string
  | undefined;

export const supabase: SupabaseClient | null =
  SUPABASE_URL && SUPABASE_ANON_KEY
    ? createClient(SUPABASE_URL, SUPABASE_ANON_KEY, {
        auth: {
          persistSession: true,
          autoRefreshToken: true,
          detectSessionInUrl: true,
          flowType: 'pkce',
          storageKey: 'obscura.supabase.auth',
        },
      })
    : null;

export const supabaseEnabled = supabase !== null;

export type SupabaseProvider = 'github' | 'google';
