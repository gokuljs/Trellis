-- Disposable PostgreSQL only. Supabase already supplies these roles and auth objects.
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname='anon') THEN
        CREATE ROLE anon NOLOGIN;
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname='authenticated') THEN
        CREATE ROLE authenticated NOLOGIN;
    END IF;
END;
$$;
CREATE SCHEMA auth;
CREATE TABLE auth.users (
    id uuid PRIMARY KEY,
    email text,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now()
);
CREATE FUNCTION auth.uid() RETURNS uuid LANGUAGE sql STABLE AS $$
    SELECT COALESCE(
        NULLIF(current_setting('request.jwt.claim.sub',true),''),
        NULLIF(current_setting('request.jwt.claims',true),'')::jsonb->>'sub'
    )::uuid
$$;
GRANT USAGE ON SCHEMA auth TO authenticated;
GRANT EXECUTE ON FUNCTION auth.uid() TO authenticated;
