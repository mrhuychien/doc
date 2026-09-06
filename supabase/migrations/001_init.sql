-- ═══════════════════════════════════════════════════════════════
-- Feed Tri Thức — Phase 1 · migration 001
-- 7 bảng + RLS (allowed_user) + seed 1 author / 1 book
-- ═══════════════════════════════════════════════════════════════

-- ─── 1. Bảng chặn truy cập (thay cho hardcode email) ───────────
create table if not exists allowed_user (
  email text primary key check (email = lower(email))
);

-- ─── 2. Domain tables ──────────────────────────────────────────
create table if not exists author (
  id          bigint generated always as identity primary key,
  slug        text unique not null,
  name        text not null,
  name_native text,
  born_year   int,
  died_year   int,
  bio_short   text,
  monogram    text
);

create table if not exists book (
  id             bigint generated always as identity primary key,
  author_id      bigint not null references author (id) on delete cascade,
  slug           text unique not null,
  title          text not null,
  title_original text,
  year_original  int,
  translator     text,
  license        text not null check (license in ('personal', 'public_domain')),
  source_file    text,
  total_chunks   int,
  pace_per_day   int not null default 5 check (pace_per_day between 1 and 20),
  status         text not null default 'active' check (status in ('active', 'paused', 'done')),
  started_on     date
);

create table if not exists chunk (
  id            bigint generated always as identity primary key,
  book_id       bigint not null references book (id) on delete cascade,
  seq           int not null,
  kind          text not null check (kind in ('context', 'excerpt')),
  chapter_no    int,
  chapter_title text,
  section_no    text,
  section_title text,
  subpoint_title text,
  part_index    int not null default 1,
  part_total    int not null default 1,
  text          text not null,   -- verbatim, không tool nào được sửa
  word_count    int,
  page_from     int,
  page_to       int,
  idea_summary  text,            -- generated (enrich.py)
  questions     jsonb,           -- [{cue, expected_answer}] x3, generated
  unique (book_id, seq)
);

create table if not exists review (
  id          bigint generated always as identity primary key,
  chunk_id    bigint not null references chunk (id) on delete cascade,
  stage       smallint not null check (stage between 1 and 3),
  due_on      date not null,
  result      text check (result in ('nho', 'mo', 'quen')),
  answered_at timestamptz
);

create table if not exists daily_plan (
  id        bigint generated always as identity primary key,
  plan_date date not null unique,
  items     jsonb not null,
  completed int not null default 0
);

create table if not exists reading_event (
  id        bigint generated always as identity primary key,
  chunk_id  bigint not null references chunk (id) on delete cascade,
  opened_at timestamptz not null default now(),
  seconds   int not null default 0
);

-- ─── 3. Index ──────────────────────────────────────────────────
create index if not exists chunk_book_seq_idx        on chunk (book_id, seq);
create index if not exists review_open_idx           on review (due_on, stage) where result is null;
create index if not exists review_chunk_idx          on review (chunk_id);
create index if not exists reading_event_chunk_idx   on reading_event (chunk_id);

-- ─── 4. RLS ────────────────────────────────────────────────────
-- Mọi bảng nội dung: chỉ email có trong allowed_user mới đọc/ghi được.
-- load.py chạy bằng service role key nên bypass toàn bộ khối này.
alter table allowed_user  enable row level security;
alter table author        enable row level security;
alter table book          enable row level security;
alter table chunk         enable row level security;
alter table review        enable row level security;
alter table daily_plan    enable row level security;
alter table reading_event enable row level security;

-- allowed_user tự soi chính mình (không đệ quy sang bảng khác)
drop policy if exists allowed_user_self on allowed_user;
create policy allowed_user_self on allowed_user
  for select to authenticated
  using (email = auth.jwt() ->> 'email');

do $$
declare t text;
begin
  foreach t in array array['author', 'book', 'chunk', 'review', 'daily_plan', 'reading_event']
  loop
    execute format('drop policy if exists %I on %I', t || '_allowed', t);
    execute format(
      'create policy %I on %I for all to authenticated
         using      (auth.jwt() ->> ''email'' in (select email from allowed_user))
         with check (auth.jwt() ->> ''email'' in (select email from allowed_user))',
      t || '_allowed', t
    );
  end loop;
end $$;

grant usage on schema public to authenticated;
grant select on allowed_user to authenticated;
grant select, insert, update, delete
  on author, book, chunk, review, daily_plan, reading_event
  to authenticated;
grant usage, select on all sequences in schema public to authenticated;

-- ─── 5. Seed ───────────────────────────────────────────────────
insert into author (slug, name, name_native, born_year, bio_short, monogram)
values ('vuong-ho-ninh', 'Vương Hỗ Ninh', '王沪宁', 1955, '', 'VH')
on conflict (slug) do nothing;

insert into book (
  author_id, slug, title, year_original, translator,
  license, source_file, pace_per_day, status
)
select a.id,
       'chong-tham-nhung',
       'Chống tham nhũng — Thí nghiệm tại Trung Quốc',
       1990,
       'Văn Dũng',
       'personal',
       'ingest/books/chong-tham-nhung/source.pdf',
       5,
       'active'
from author a
where a.slug = 'vuong-ho-ninh'
on conflict (slug) do nothing;
-- book.total_chunks do load.py cập nhật.
