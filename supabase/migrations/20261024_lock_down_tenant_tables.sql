-- Keep tenant data accessible only through the trusted server-side service-role client.
-- The application reads/writes these tables through FastAPI using SUPABASE_SERVICE_ROLE_KEY;
-- browser clients should not access them directly. Enabling RLS plus revoking direct grants
-- prevents accidental exposure if a broad policy or grant was previously configured.
BEGIN;

ALTER TABLE public.businesses ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.products ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.orders ENABLE ROW LEVEL SECURITY;

REVOKE ALL PRIVILEGES ON TABLE
    public.businesses,
    public.products,
    public.orders
FROM PUBLIC, anon, authenticated;

GRANT ALL PRIVILEGES ON TABLE
    public.businesses,
    public.products,
    public.orders
TO service_role;

COMMIT;
