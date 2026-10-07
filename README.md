# antlia

The data layer. *Antlia*, the air pump, moves what is in there out to here
without altering it — this pulls market data out of vendor APIs and
makes it available in one shape, and resists improving anything on the way
through.

It is a library. Other projects import it; it is the one place they get data
from, so a backtest, a screen and a dashboard cannot quietly disagree about what
happened.

Data is laid out on two axes. **Access** decides what a read promises:
`history` is fixed (given `as_of`, the same rows forever) and cached;
`live` is what a source says right now, stamped and never stored. **Asset
class** decides what a row is, and is shared by both: a live row and a
history row for the same instrument carry the same identity columns
(`antlia.schema`).

| asset class | `history` | `live` |
|---|---|---|
| eq | `equity_eod` (ThetaData) | — |
| option | `option_eod`, `expirations` (ThetaData) | — |
| fx | — | `live.fx` (Yahoo, then the ECB fixing) |
| rate | `rate_daily` (FRED) | — |

Beneath them, two pieces of infrastructure that return no market data:
`auth` (credentials, sessions, rate limits) and `gateway` (where a source's
gateway is, and how to restart it).

## `antlia.auth`

Gives you a working client for a source, and keeps everything that goes into
getting one out of the calling code.

```python
from antlia import auth

with auth.session("ibkr", profile="paper") as ib:
    positions = ib.positions()

with auth.session("trading212") as t212:
    portfolio = t212.get("/api/v0/equity/portfolio").json()
```

What it owns: where credentials come from, when connections open and close,
whether two callers share one, and how fast a source may be hit. What it does
not own: how you call the vendor's client — `session()` yields the vendor's own
object (`ib_insync.IB`, `httpx.Client`, …), not a wrapper.

### Sources

| source | shape | profiles | extra |
|---|---|---|---|
| `fred` | free API key, plain HTTPS | — | none |
| `ibkr` | socket to a running TWS / IB Gateway | `paper` (7497), `live` (7496) | `antlia[ibkr]` |
| `thetadata` | API key, or email+password (cloud gRPC) | — | `antlia[thetadata]` |
| `trading212` | key + secret, HTTP Basic | `live`, `demo` | `antlia[trading212]` |
| `yfinance` | unauthenticated | — | `antlia[yfinance]` |

Each vendor SDK is an optional extra, imported the first time its source is
used. One adapter's dependency is never everyone's install cost, and a missing
one produces `MissingExtra: source 'ibkr' needs the 'ib_insync' package: pip
install 'antlia[ibkr]'` rather than an ImportError from three frames down.

### Configuration

Environment first, then `~/.antlia/credentials.toml`:

```
ANTLIA_<SOURCE>_<PROFILE>_<FIELD>     e.g. ANTLIA_IBKR_PAPER_PORT
ANTLIA_<SOURCE>_<FIELD>               e.g. ANTLIA_THETADATA_PASSWORD
[<source>.<profile>] <field>
[<source>] <field>
spec default
```

Environment wins so a shell or a CI job can override a habit without editing
anything; the file exists because profiles turn into an unreadable pile of
environment variables fast. `python -m antlia.auth --init` writes a commented
starter file with mode 600.

Deliberately not a keyring: on WSL2 there is no Secret Service daemon, and
`keyring` there either fails or silently falls back to a plaintext backend —
which is this file, with extra steps and less clarity about what is actually
protecting the secret. What protects it is file permissions, and antlia warns
when they are loose.

A missing required field names every place it looked:

```
MissingCredential: no value for trading212.api_key; looked in, in order:
  $ANTLIA_TRADING212_LIVE_API_KEY
  $ANTLIA_TRADING212_API_KEY
  [trading212.live] api_key in /home/you/.antlia/credentials.toml
  [trading212] api_key in /home/you/.antlia/credentials.toml
```

### Connections

Pooled by identity and kept warm past the `with` block, until `auth.close()`,
`auth.close_all()`, or interpreter exit. These are long-lived sessions to a
local daemon, where reconnecting in a loop is slow and — for IBKR — a way to
burn through client ids. Two callers agreeing on the identity share one
connection; two profiles, or two accounts at one broker, never collapse into
one. A dead handle is detected and replaced.

IBKR connects **read-only by default**, so the gateway itself rejects order
placement on antlia's socket. Antlia has no execution path, and this makes that
a broker-enforced fact rather than something a reviewer has to notice.

### Rate limits

One token bucket per source/profile, shared by every session of that identity —
the vendor's limit belongs to the account, not to the caller. Set `rate_limit`
(calls/sec) in the config to match your plan. Where the SDK's calls cannot be
intercepted, pace them yourself:

```python
limiter = auth.limiter("yfinance")
for symbol in universe:
    limiter.acquire()
    ...
```

### Nothing here authenticates until you ask it to

`auth.credential(...)` resolves settings without opening anything, and no
adapter is imported until its source is used. A cache-covered backtest runs
start to finish without ever touching a vendor.

### Three levels, and they are different claims

| | means | proves |
|---|---|---|
| resolves | the settings were found | nothing about the credential |
| connects | a client object was built | little — an `httpx.Client` with a wrong key builds fine |
| verifies | one cheap real round-trip came back | the credential works |

`verify()` per source: IBKR asks the gateway for its managed accounts;
Trading212 calls `account/info` and translates 401/403/429 into what to do about
them; ThetaData lists AAPL expirations, separating "auth failed" from "the data
channel is down"; yfinance pulls one daily bar (Yahoo answers an unavailable
endpoint with an empty frame, so emptiness is the failure).

```bash
python -m antlia.auth                  # what resolves, which SDKs are installed
python -m antlia.auth --connect        # also open a session
python -m antlia.auth ibkr --verify    # ...and prove it works
python -m antlia.auth ibkr -p live     # one profile only
python -m antlia.auth --init           # write a starter credentials.toml
```

### Debugging a source

`scripts/probe.py` resolves, connects, verifies, then breaks with the live
client bound to `handle`:

```bash
python scripts/probe.py ibkr --profile paper
python scripts/probe.py trading212 --no-repl
python scripts/probe.py ibkr --set port=4002      # override a resolved field
```

`.vscode/launch.json` (local-only) has one configuration per source running
exactly that, with `justMyCode: false` so breakpoints in
`src/antlia/auth/providers/` hit and you can step into the vendor SDK.

### Adding a source

Declare a `SourceSpec` — fields, extra, profiles, rate — and implement
`connect`. Resolution order, pooling, limiting and lifecycle are already done.

```python
class MyProvider(auth.Provider):
    spec = auth.SourceSpec(
        name="mine",
        fields=(auth.Field("api_key", secret=True),),
        extra="mine",
        package="mysdk",
        rate=5.0,
    )

    def connect(self, cred, limiter):
        return self.require("mysdk").Client(cred["api_key"])

    def disconnect(self, handle):
        handle.close()


auth.register("mine", MyProvider())
```

## `antlia.live.fx`

Foreign exchange rates, so figures in different currencies can be added up.
A live read: the current rate table, cached for six hours so a dashboard does
not refetch it per render. `antlia.fx` is the same module under the name it
shipped with.

```python
from antlia.live import fx

table = fx.rates(("USD", "GBP", "JPY", "HKD"))
table.convert(83289.64, "GBP", "USD")  # -> 112753.17
table.source, table.as_of, table.stale  # 'yfinance', ..., False
```

`RateTable.rates` is **units of each currency per 1 USD** — `GBP: 0.7387,
JPY: 160.04` reads as "one dollar buys 0.7387 pounds or 160.04 yen". USD is the
pivot and every conversion crosses through it, so adding a currency is a
one-line change.

```bash
python -m antlia.live.fx                        # the table, cache or live
python -m antlia.live.fx --verify               # every source, live, in turn
python -m antlia.live.fx --convert 83289.64 GBP USD
```

### Two sources, tried in order

**`yfinance` first.** `{CCY}=X` is a market rate, and it reconciles with the
broker: Yahoo said USD/JPY 160.04 where IBKR's own reported rate was 160.08, and
a JPY 772,600 position converts to USD 4,827.60 against IBKR's own USD 4,826.
That agreement is the point — a converted total that cannot be reconciled with
the broker's own screen is worthless.

**`frankfurter` underneath it** (ECB reference rates, no key, no quota). Official
and stable, but a once-daily 16:00 CET fixing: on 2026-08-28 it had GBP/USD at
0.73624 against the market's 0.73869, which is 0.33% — small enough to look right
and large enough to break a reconciliation. It is second for that reason, and
present because a chain whose only link is an unofficial scraper is not a chain.

Sources are tried in order and **the first complete answer wins**. Results are
never stitched together across providers: a total assembled from two vendors'
rates cannot be reconciled against either of them. `fx.chain()` shows the order;
`fx.register(name, source, first=True)` puts your own at the head of it.

### What it will and will not do

- **It never invents a rate.** An unknown currency converts to `None`, never to
  an approximation. A wrong rate mis-states a whole portfolio quietly; an absent
  one just removes a feature.
- **It caches with a TTL and keeps the stale copy.** Rates are cacheable in a way
  account state deliberately is not. The table lives in `~/.antlia/fx.json` for
  six hours; when every source fails, the last one comes back with `stale=True`
  rather than nothing. The consumer decides whether stale is good enough — but
  it is told. The TTL is measured from the fetch, not from `as_of`: Yahoo stamps
  an FX rate with the last daily close, so a TTL keyed to that would expire
  instantly and never once hit.
- **It raises rather than returning `None`.** `RatesUnavailable` — a subclass of
  `auth`'s `ConnectionFailed` — when there is nothing at all, not even a stale
  table. Degrading to "no rates" is the consumer's policy, not this layer's.
- **A broker's rate is not an FX source.** See below.

### A broker's rate is not an FX source

A broker reports its own rate per currency, live, and by
definition the one its account totals were computed with. That number is **not**
reachable through `fx`, on purpose:

1. It is not a market rate. It is what *one* broker used to value *one* account
   at *one* instant, and it means something only against that account's totals.
2. It cannot answer this layer's question. `rates()` takes currencies; a broker
   rate needs a credential, a connection and an account id. Admitting one would
   force an `account=` argument or a silent "first account" — and then the same
   call returns different numbers depending on configuration, which is exactly
   the failure "single source of truth" is meant to prevent.
3. The two genuinely differ (160.04 against 160.08). One call with two answers
   is worse than one answer and a documented way to compare the other.

So: **restating a broker's own account totals uses that broker's own rate;
combining across brokers, or converting anything that is not an account, uses
`fx`.** To compare them, `RateTable.inverse()` is deliberately in the broker's
orientation — units of base per 1 unit of the currency, which is how
IBKR reports it:

```python
table.inverse("JPY")  # 0.006248 -- the market
# 0.006247 -- what IBKR valued the account with
```

Comparing across orientations instead is wrong by a factor of 25,000 and looks
plausible in neither direction, which is why the accessor exists at all.

## `antlia.history`

Historical market data, cached locally, in one shape whichever vendor answered.
This is what a backtest reads.

```python
from antlia import history

history.equity_eod("AAPL", "2026-08-17", "2026-08-28")
history.option_eod("AAPL", "2026-08-10", "2026-08-28", dte=(30, 45))
history.coverage("option_eod", "AAPL", "2026-08-10", "2026-08-28")

# A read takes a universe; one frame, and the `symbol` column tells them apart.
history.equity_eod(["AAPL", "MSFT"], "2026-08-17", "2026-08-28")

# Interest rates, by antlia's series name: annualised decimals (0.0422 = 4.22%).
history.rate_daily(["UST_3M", "SOFR"], "2026-01-01", "2026-09-30")
```

Naming no source reaches the first one that serves the table: ThetaData for
`equity_eod` and `option_eod`, FRED for `rate_daily`.

Reads return a **`pyarrow.Table`**; `frame="pandas"` or `frame="polars"`
converts, and imports that library only when you ask for it.

```bash
antlia-history                                    # what is held
antlia-history verify                             # one live round-trip
antlia-history plan option_eod AAPL --start 2026-06-01 --end 2026-08-28 --max-dte 45
antlia-history fill option_eod AAPL --start 2026-06-01 --end 2026-08-28 --max-dte 45
antlia-history check --repair                     # is the store intact?
```

`plan` prints how many vendor requests a `fill` would make and costs none of
them. On a metered plan, run it first.

`antlia-history` is a console script installed with the package -- warming a
cache is a batch job you run from a shell or a cron entry, not from inside a
backtest. From a source checkout with no install, `python -m antlia.history.cli`
runs the same command.

### Cache-first, and it is enforced

A window the store already holds costs **zero vendor requests and never
authenticates**. That is the property that makes a backtest repeatable and, on a
metered plan, affordable: 14,260 option rows come back in 0.4s with no session
opened. There is a test that fails if a covered read so much as resolves a
credential.

What is missing gets fetched, appended and recorded, so the second run of the
same backtest is free. `fetch=` decides what happens when the store falls short,
because both answers are right at different times:

```python
history.option_eod("AAPL", start, end)  # fill the gaps (default)
history.option_eod("AAPL", start, end, fetch=False)  # NotCovered, naming the gaps
```

`ANTLIA_HISTORY_FETCH=0` flips the default process-wide, which is how a run is
made provably offline without editing its call sites.

### One write path, and a ledger of what was asked

Everything that lands in the store goes through `history.fill()`. It plans
before it spends anything:

1. **Clamp to the entitlement horizon.** ThetaData fails a request whose range
   *straddles* its plan boundary rather than trimming it, so reaching too far
   back costs the whole fetch.
2. **Subtract what is already settled** — rows fetched *and* days proven empty.
3. **Subtract what the source has already refused.** A permanent refusal is
   recorded once and never paid for again.
4. **Clamp each expiration to its own life.** March's expiration is never asked
   about April. On a multi-year warm-up this is the largest saving there is.
5. **Split by the vendor's span cap**, exactly.

The ledger is why step 2 can say "proven empty": a date with no rows is
otherwise ambiguous between a market holiday and a fetch that never happened,
and without somewhere to write that down a cache-first reader re-fetches every
holiday forever.

### The store

    ~/.antlia/store/raw/<source>/<table>/session_date=YYYY-MM-DD/*.parquet
    ~/.antlia/store/ledger/*.parquet

`$ANTLIA_STORE` moves it; `store=` moves it for one call. DuckDB reads the
Parquet directly — no server, no load step.

**`raw/` is immutable and append-only and holds the vendor's own columns.**
Nothing is renamed, converted, dropped or repaired on the way in — including the
vendor's own float noise, which is stored as received rather than rounded into
something tidier. Two provenance columns are added, `source` and `ingested_at`,
and a vendor restatement is a **new append**, never an edit:

```python
history.equity_eod("AAPL", start, end)  # newest per session
history.equity_eod("AAPL", start, end, latest=False)  # every version, ever
```

Normalisation happens **at read time**, in SQL, from the source's own
projection. That is what lets the read API be one schema while `raw/` stays
vendor-native, and it is why a vendor's mistake can be re-mapped later instead of
having been baked into the files.

### Restatements, and reproducing a past run

A vendor changes its mind. `refresh=` is the only thing that records that — every
other path subtracts what the ledger already settles, so without it a key can
never have a second version:

```python
history.fill("option_eod", "AAPL", start, end, refresh=True)  # append a new version
history.option_eod("AAPL", start, end)  # the newest, per contract
history.option_eod("AAPL", start, end, latest=False)  # every version, ever
history.option_eod("AAPL", start, end, as_of=when)  # what it said at `when`

history.ingests("option_eod", "AAPL")  # the moments `as_of` accepts
```

Every row carries `ingested_at` -- **when that row was downloaded**, as distinct
from the session date it describes, which is what `raw/` partitions on. `as_of`
cuts on the download stamp and defaults to the newest.

`ingests()` exists because `as_of` wants a timestamp and nothing otherwise tells
you which ones there are. It is **one entry per write, not per fill**: a
forty-expiration warm-up leaves forty, so `as_of` half way through a run shows
exactly the rows that existed half way through it. Stamping a whole run with its
start time would read tidier and would show you rows from the future.

Nothing is ever edited. `as_of` is what makes a study reproducible after the
vendor restates: verified live — a chain re-fetched with `refresh=True` doubled
the stored appends, `latest=True` returned the new numbers, and `as_of` the
first ingest returned the old ones, row for row.

### Faster warm-ups

`workers=` (`--workers`) runs several requests at once. They are independent by
construction and the rate limiter is process-wide, so it goes faster without
going over budget:

```
6 requests, 1 worker  -- 13.3s
6 requests, 2 workers --  6.0s
```

**The vendor's ceiling is declared, not guessed.** ThetaData's FREE plan serves
two concurrent requests and refuses the third outright, so the adapter declares
`max_workers = 2` and asking for eight quietly gets two — the same way a
400-day window quietly becomes 365-day chunks. A source `auth` marks
single-threaded is clamped to one regardless. The default is 1, because raising
it spends a metered budget faster and that is your call.

### Integrity, and the line it does not cross

```bash
antlia-history check [--repair]
```

`check` asks whether the **store is telling the truth about itself** — never
whether the numbers in it are any good. Three faults, and they are different:

- **An unreadable file.** The worst, because the reader unions the whole glob to
  align schemas: one truncated file makes *every* date in that table unreadable,
  not just its own. `--repair` quarantines it (never deletes it — it is the only
  copy of what the vendor said) so reads work again, then you re-fetch with
  `--refresh`.
- **A leaked staging directory**, left by a write that was killed.
- **An expiration listing older than the option data beside it** -- the
  store-wide form of the coverage rule above: sessions were fetched that the
  listing had never heard of.
- **A count mismatch** between what the ledger says was fetched and what is on
  disk. The direction tells you what happened: *store < ledger* means rows were
  lost after being recorded; *store > ledger* means rows were written and never
  recorded, which is what a `SIGTERM` between the write and the flush produces.

Writes are staged and renamed into place, so a killed process leaves whole files
or none. `check` finds damage from anything that got there another way.

### Completeness, not quality

`coverage()` answers whether every day in a window is accounted for. It never
judges whether the prices are any good — a crossed quote or a mid below
intrinsic is a judgement call that belongs to the consumer, not to one
researcher's opinion baked into everybody's data.

```
AAPL option_eod via thetadata for 2026-08-10 .. 2026-08-28: 17064 rows, complete
```

`missing` was never fetched. `denied` the source refused and will refuse again.
Keeping them apart is what stops a fetch loop retrying a permanent answer.

**An option answer is only as good as the expiration listing behind it.** The
plan divides work by that listing, so a gap in an expiration the listing never
knew about cannot appear in `missing`. The test is exact rather than a guess at
a TTL, and it rests on one fact: **an expiration is always listed before it
trades**, and the vendor's listing is historical.

    listed_at >= window.end   ->  the listing knows every expiration that traded
    listed_at <  window.end   ->  expirations may have been added since

So a store warmed in March vouches for a January backtest and does not vouch for
a September one:

```
AAPL option_eod ... for 2026-01-05 .. 2026-01-09: 10 rows; cannot vouch for this
window -- the expiration listing was last fetched 2026-01-03, so expirations
listed since are unknown; re-run fill to refresh it
```

`Coverage.complete` is False there even with nothing in `missing`. A TTL would
be wrong in both directions: it would call last night's store stale this
morning, and say nothing at all about a six-month-old one.

### ThetaData, and what a FREE plan actually serves

Measured against a live key on 2026-08-30, not read off a docs page:

| | |
|---|---|
| `stock_history_eod`, `option_history_eod` | works — OHLCV plus the closing NBBO |
| `option_list_expirations` / `_strikes` | works |
| everything intraday | `PERMISSION_DENIED` |
| open interest, vendor greeks, flat files | `PERMISSION_DENIED` |

So the adapter serves EOD and only EOD — that is the entire free entitlement,
and a table it cannot fill is better absent than half-populated. Four limits
shape the planner:

- **A rolling history horizon.** A FREE plan reached back to **2023-07-10** when
  measured on 2026-08-30. Crossing it fails the whole request, so the plan
  clamps first; `antlia-history horizon` re-measures it in about
  eleven requests when the window has moved.
- **365 days per request, inclusive.** 366 is an `INVALID_ARGUMENT`.
- **A non-trading day raises rather than returning an empty frame.** Treating
  that as a failure would abort a run on every market holiday.
- **`strike="*"` fetches a whole expiration in one call** — a few hundred rows a
  second, which is the real cost of a warm-up: about 35s for one expiration over
  a 90-day window.

### Sessions are fetched only once they are published

A vendor asked about a session it has not summarised yet answers for the rest
of the window and says nothing about that day. Each source therefore declares
`latest()`, the newest session it has published, and the plan stops there:
ThetaData's EOD row counts as published at 18:00 New York, and a FRED rate two
business days after the last finished business day (rates are published the
next afternoon). A fill run before then leaves today unfetched rather than
recording it as held, `Plan` names the span as not yet published, and
`coverage()` counts it as missing.

### FRED, for interest rates

`rate_daily` comes from FRED's API, which needs a free key
(fred.stlouisfed.org/docs/api/api_key.html) under `[fred] api_key` in
`~/.antlia/credentials.toml`, and nothing installed beyond antlia. Series are
named by antlia -- `UST_1M`, `UST_3M`, `UST_6M`, `UST_1Y`, `UST_2Y`, `UST_10Y`
(Treasury constant maturity), `TBILL_3M` (discount basis), `SOFR`, `EFFR` -- and
mapped to FRED's ids inside the adapter. `raw/` keeps FRED's observations as
sent, `"."` for a missing value included; that reads back as NULL, not zero.

### What this data is, and is not

Measured, not assumed. Read this before a backtest believes anything.

**Prices are unadjusted.** `raw/` stores what the vendor sent, and ThetaData
sends as-traded prices. Across NVDA's 10:1 split on 2024-06-10 — inside the
free plan's own window — the series does this:

```
NVDA  2024-06-07  close 1208.88
NVDA  2024-06-10  close  121.79
```

Unadjusted is the *right* thing for `raw/` and often the right thing for
options work, because the contract terms were adjusted too. But **antlia
currently ships no corporate-actions table and issues no warning**, and
ThetaData 1.0.x has no endpoint for one (66 methods, none of them splits or
dividends). Until that gap is filled, a naive return series over any symbol
that split in the window is wrong and nothing will tell you.

**The closing quote is a post-close snapshot.** `stamp` lands at 17:15–17:18
New York, about 75 minutes after the equity-option close. The quotes are
usable, not fictional — but they are not the 16:00 print, and filling at their
mid is not a fill you could have got. Measured on one week of AAPL:

| mid price | rows | median spread |
|---|---|---|
| < $0.10 | 315 | 100% |
| $0.10–1 | 396 | 24.7% |
| $1–5 | 290 | 9.4% |
| > $5 | 2092 | 6.3% |

Of 4,254 chain rows, 1,161 had a zero bid and 1,931 no volume at all — normal
for a full chain, and exactly the judgement call antlia leaves to you. **Zero
rows were crossed**, so the data is clean in the way that matters.

**Not available on the free plan**: open interest, vendor greeks and implied
volatility, anything intraday. No open interest means liquidity can only be
filtered on same-day volume, which is not the same question. No greeks means
antlia alone cannot serve an IV strategy — and antlia may not compute them
(that is a feature definition, and out of bounds here).

**Also absent, by design or by gap**: a trading calendar (so `complete` means
"every day is accounted for", not "every trading day has rows"), a spot-to-chain
join (two tables; you join them), and delisted symbols (no survivorship-free
universe).

### Looking at what is cached

```bash
antlia-history                    # store root, files and symbols per table
python scripts/history_probe.py option_eod AAPL --start 2026-08-10 --end 2026-08-28
```

The probe prints `coverage()` and then breaks in the debugger with the rows
bound three ways — `table` (pyarrow), `df` (pandas), `pf` (polars) — so you can
check both halves of the question at once: is it cached, and can it be handed to
a consumer. It fetches nothing unless given `--fetch`.

### Adding a source

The package is laid out so the central rule is visible: `reads.py` holds the
read surfaces, `writes.py` the one write path, `listing.py` the expiration
listing they both plan from, and `__init__.py` nothing but re-exports.

A `HistorySource` supplies four things: what it can serve, how work divides into
scopes, how to fetch one scope (returning **the vendor's frame, unmodified**),
and a `{canonical column: SQL expression}` projection. The reader, the cache,
the coverage ledger and every consumer stay exactly as they are — no consumer
can tell which vendor answered.

## `antlia.gateway`

Most sources are an HTTP endpoint and a key. One is a **desktop application in
a container**, and while it is not logged in, every layer above it is dark.
This module is the small amount of knowledge needed to diagnose and fix that.

```python
from antlia import gateway

info = gateway.describe("ibkr", "live")
if info is None:
    ...  # a REST API: there is nothing to look at
else:
    info.screen_url  # noVNC page, password already filled in
    info.vnc_addr  # "127.0.0.1:5900", for a native viewer
    info.control  # did IBC's command server answer just now

gateway.restart("ibkr", "live")  # -> IBC's own words, verbatim
```

```bash
python -m antlia.gateway              # every gateway, and whether it answers
python -m antlia.gateway -s ibkr -p live --restart
```

`None` is the whole protocol for "this source has no gateway", so no consumer
keeps its own list of which ones do. A name no registry knows raises
`UnknownSource` instead — a typo must not read as "nothing to see here".

### It cannot log a gateway in, and no version will

IB Gateway has **no headless mode and no login API**. The only way in is a
Swing dialog; the only thing that types into it is IBC; and IBC has to be
inside the container. So there is no `TWS_PASSWORD` here and nowhere to put
one.

```
IBC (in the container) --drives the login dialog--> IB Gateway
                             --and only then opens--> :4001 --> antlia.auth
```

That is why the IBKR settings antlia resolves are `{host, port, client_id,
readonly, timeout}` — endpoints, with not one secret among them. Trading212's
`api_key`/`api_secret` look like the same kind of thing and are not: those are
real API credentials, they belong in `auth`, and merging the two cases would
put a broker password somewhere it has no business being.

What *is* reachable from outside the container is a picture of the screen and a
robot that will restart it, and that is exactly what this module exposes.

### `restart()` is a soft restart, not a fresh login

IBC implements it by setting the gateway's own auto-restart a minute ahead —
its log says `Setting auto-restart time to 11:42 AM` — which is IBKR's
**session-preserving** restart. It does not re-authenticate and it does **not**
push a new two-factor notification. Documentation claiming otherwise was
written once here and disproved by the gateway's own log.

It also needs a UI that can respond: behind a modal dialog, IBC sits on
`Waiting for config dialog future to complete` indefinitely, and the call times
out. The two failures are distinguished in the message, because a refusal means
the command server was never switched on and a timeout means it was.

Errors are prose for a person. `ControlUnavailable` subclasses
`ConnectionFailed`, so `except ConnectionFailed` still covers it; branch on
whether it raised, never on the words, and show the words to the reader.

### The command server being off is a normal answer

IBC ships `CommandServerPort=0`, so most gateways have no control channel and
`control=False` is what a correct, healthy, unmodified deployment looks like.
`describe()` returns it as a field rather than raising.

`control` is probed on every call, because it is a claim about *right now* and
a stale "yes" sends someone to a button that cannot work. The screen is
deliberately not probed: the browser is the better detector, and waiting on a
second socket doubles the latency of an answer nobody acts on.

### The VNC password is filled into the URL, and must not be persisted

noVNC 1.4 reads `password` from the query string and, with `autoconnect=1`,
then never draws its credential dialog. That is worth doing for a reason that
has nothing to do with saving a keystroke: the dialog is an ordinary
`<input type="password">` in an ordinary web page, so password managers and
keyboard extensions fight the user for it — inside an iframe as much as
outside, since extensions inject into every frame. Prefilling removes the field
rather than winning the fight.

This is the one secret the layer holds, and it protects a view of a screen, not
an account. The cost is a secret living in a string that looks like
configuration, so the redaction is built into the type rather than left to each
caller:

```python
info.screen_url  # carries the password — hand it to a browser, keep no copy
info.safe_screen_url  # the same link, asking for it — safe to log or save
repr(info)  # redacted
info.redacted()  # redacted
```

It resolves from `[ibkr] vnc_password`, `$ANTLIA_IBKR_LIVE_VNC_PASSWORD`, and
finally the plain `$VNC_SERVER_PASSWORD` — the gateway compose project's own
spelling, so `set -a; . ~/ib-gateway/.env; set +a` is enough to supply it.
Nothing here reads that project's files; the variable is the whole contract.

### Configuration, and the host side of those ports

Settings live in the source's own section, beside the ones `auth` already
resolves from there — one broker, one place:

```toml
[ibkr]
host = "127.0.0.1"     # the same field auth reads, so a remote gateway is one edit
control_port = 7462    # IBC's command server; 0 means "not published"

[ibkr.live]
screen_port = 6080     # the noVNC bridge
vnc_port = 5900        # the VNC server itself
```

Any port set to `0` means not published, and that field comes back `None`
rather than pointing at something that is not there.

The compose override that publishes those ports, the vendored IBC template that
switches the command server on, and the traps involved in both, are in
[`ops/ib-gateway/`](ops/ib-gateway/) with their own README. Nothing in there is
a secret; the credentials stay in the gateway project's own `.env`, outside any
repository.

**No extras and no vendor SDK.** Like `auth`, this runs on the standard library
— which is the point, because a gateway is asked about precisely when the
broker's own connection is in doubt.

## Using antlia from another project

```bash
uv add "antlia[store,thetadata] @ /path/to/antlia"   # or a git URL
```

Then, from anywhere — credentials live in `~/.antlia/`, so nothing depends on
the working directory:

```python
from antlia import history

bars = history.equity_eod("AAPL", "2026-08-17", "2026-08-28", frame="pandas")
```

Three things to know before wiring it in:

- **Python >=3.12**, and ask for the extras you use. A plain `pip install
  antlia` gives you credential resolution and no vendor SDKs at all.
- **Rate limiting is per process.** The token buckets live in memory, so two
  processes hitting the same account share nothing and can collide. Trading212
  requests retry once at the reset the vendor names; beyond that, one long-lived
  process is the design point.
- **A source that cannot be reached raises**, it does not return empty. IBKR
  needs its gateway running; a missing SDK raises `MissingExtra` naming the
  exact `pip install`.
- **`fx` needs no extra at all.** Its Frankfurter fallback runs on the standard
  library, so a plain `pip install antlia` can still convert currencies;
  `antlia[yfinance]` upgrades the primary source from a daily fixing to a market
  rate.
- **`history` needs `antlia[store]`** for DuckDB and pyarrow, plus the extra for
  whichever vendor fills it (`antlia[store,thetadata]`). Reading a warmed store
  needs no vendor extra at all — a machine that only runs backtests never has to
  install the SDK.
- **`gateway` needs no extra either**, deliberately: it answers "is the gateway
  up, and how do I look at it" without `ib_insync` installed, which is the state
  you are most likely to be in when you need to ask.

## Development

```bash
uv sync --extra dev --extra store
uv run pytest
uv run ruff check . && uv run ruff format --check . && uv run mypy
```

Python 3.12+. The core has no required dependencies — a consumer that wants only
`antlia.auth` is not made to install a query engine.
