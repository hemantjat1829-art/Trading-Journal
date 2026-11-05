-- Quotex affiliate journal: v0 schema (PostgreSQL 12+)
-- Importer and postback fields are provisional until we see a real export and a real postback payload.

create table users (
  id               bigserial primary key,
  email            text unique not null,
  password_hash    text not null,              -- drop this if you use Supabase Auth
  quotex_trader_id text unique,                -- the ID shown on the user's Quotex account page
  verified_at      timestamptz,                -- set when trader ID matches referred_traders
  timezone         text not null default 'Asia/Kolkata',   -- heatmap days are cut in the user's timezone
  consented_at     timestamptz,                -- privacy-policy consent
  created_at       timestamptz not null default now()
);

-- Filled by the postback endpoint, or by importing the referred-trader list from the partner panel.
create table referred_traders (
  quotex_trader_id text primary key,
  click_id         text,                       -- your sub-ID / click ID, if the link supports one
  registered_at    timestamptz,
  first_deposit_at timestamptz,
  raw              jsonb,                      -- keep the whole payload until the fields are confirmed
  created_at       timestamptz not null default now()
);

create table trade_imports (
  id          bigserial primary key,
  user_id     bigint not null references users(id) on delete cascade,
  filename    text,
  rows_total  int,
  rows_new    int,
  imported_at timestamptz not null default now()
);

create table trades (
  id           bigserial primary key,
  user_id      bigint not null references users(id) on delete cascade,
  import_id    bigint references trade_imports(id) on delete set null,
  account_type text not null check (account_type in ('live', 'demo')),  -- never mix demo into live stats
  asset        text not null,
  is_otc       boolean not null default false,
  direction    text check (direction in ('up', 'down')),
  currency     char(3) not null,               -- store as shown (INR vs USD); never sum across currencies
  amount       numeric(14,2) not null,         -- stake
  profit       numeric(14,2) not null,         -- NET = export's Profit column minus Amount (a loss = -stake)
                                               -- the export's IP column is deliberately not stored
  result       text not null check (result in ('win', 'loss', 'draw')),  -- draw = stake refunded, profit 0
  opened_at    timestamptz not null,
  closed_at    timestamptz,
  external_id  text not null,                  -- Quotex's own trade ID (UUID shown in the export)
  payout_pct   numeric(5,2),                   -- e.g. 85
  open_price   numeric(18,6),
  close_price  numeric(18,6),
  unique (user_id, external_id)                -- re-uploading the same file adds nothing
);

create index trades_user_closed_idx on trades (user_id, closed_at);

-- One row per user, account type, currency and local day: feeds the heatmap and daily accuracy.
create view daily_stats as
select
  t.user_id,
  t.account_type,
  t.currency,
  (t.closed_at at time zone u.timezone)::date                  as day,
  count(*)                                                     as trades,
  count(*) filter (where t.profit > 0)                         as wins,
  count(*) filter (where t.profit < 0)                         as losses,
  sum(t.profit)                                                as pnl,
  round(100.0 * count(*) filter (where t.profit > 0)
        / nullif(count(*) filter (where t.profit <> 0), 0), 1) as accuracy_pct
from trades t
join users u on u.id = t.user_id
where t.closed_at is not null
group by 1, 2, 3, 4;
