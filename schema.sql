-- toolcut multi-tenant schema. Run in the Supabase SQL editor.

create table if not exists public.jobs (
  id           uuid primary key default gen_random_uuid(),
  user_id      uuid not null references auth.users(id) on delete cascade,
  status       text not null default 'processing'
               check (status in ('processing','done','error')),
  paper        text,
  tool_w_mm    numeric,
  tool_h_mm    numeric,
  cutout_w_mm  numeric,
  cutout_h_mm  numeric,
  svg_key      text,
  dxf_key      text,
  debug_key    text,
  error        text,
  created_at   timestamptz not null default now()
);

create index if not exists jobs_user_created_idx
  on public.jobs (user_id, created_at desc);

-- Row Level Security: a signed-in user can only ever see/insert their own rows.
-- (The backend uses the service_role key, which bypasses RLS, and sets user_id
--  itself from the verified JWT. These policies protect DIRECT client access,
--  e.g. your Expo app reading job history straight from Supabase.)
alter table public.jobs enable row level security;

drop policy if exists "jobs_select_own" on public.jobs;
create policy "jobs_select_own" on public.jobs
  for select using (auth.uid() = user_id);

drop policy if exists "jobs_insert_own" on public.jobs;
create policy "jobs_insert_own" on public.jobs
  for insert with check (auth.uid() = user_id);
