# antlia

The data layer. *Antlia*, the air pump, moves what is in there out to here
without altering it — this pulls market and account data out of vendor APIs and
makes it available in one shape, and resists improving anything on the way
through.

It is a library. Other projects import it; it is the one place they get data
from, so a backtest, a screen and a dashboard cannot quietly disagree about what
happened.

Five parts, of which three are built:

| | | |
|---|---|---|
| `auth` | credentials, sessions, rate limits | **built** |
| `account` | live account state, multi-source | **built** (IBKR, Trading212) |
| `fx` | foreign exchange rates, multi-source | **built** (Yahoo, ECB) |
| `history` | cached historical data, multi-source | planned |
| `live` | on-demand live historical pulls | planned |

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

## `antlia.account`

Live account state — positions, balances, margin, working orders, fills — in one
canonical schema whichever broker it came from.

```python
from antlia import account

snap = account.snapshot("ibkr", profile="live")
snap.margin.excess_liquidity  # headroom, in the account's base currency
snap.margin.utilisation  # maintenance / net liquidation
snap.balance("JPY").cash  # per-currency cash
[p for p in snap.positions if p.instrument.kind == "option"]
```

`positions()`, `balances()`, `margin()`, `orders()`, `fills()` are views of the
same snapshot. Authentication and the socket belong to `antlia.auth`; this layer
only maps. It reads and only reads — reading working orders is in scope, placing
one never is, and the IBKR session connects `readonly=True` so the broker
enforces it.

```bash
python -m antlia.account ibkr -p live          # a readable table
python -m antlia.account ibkr -p live --json   # the same thing, machine-readable
```

### The normalisation that matters

**`average_price` and `market_price` are on the same scale.** Brokers do not
guarantee this: IBKR reports `avgCost` *including* the contract multiplier while
`marketPrice` excludes it, so an option bought at 2.90 arrives as `290.04`
beside a market price of `3.50`. Comparing those two is a mistake every consumer
would otherwise make exactly once. `Position.cost_basis` puts the multiplier
back when you want money.

Other decisions the canonical schema makes:

- **A multi-currency account reports a `BASE` pseudo-currency** alongside the
  real ones. It is a consolidated rollup — `Balance.is_consolidated` flags it,
  and summing without excluding it double-counts.
- **Margin is denominated in the account's base currency**, read off
  `NetLiquidation` rather than the `Currency` tag (which appears once per
  currency held, so picking one is a coin flip that mislabels every figure).
- **An unreadable value is `None`, never `0.0`.** A zero `day_trades_remaining`
  is a different and much more alarming claim than "not reported".
- **`Instrument.ids` keeps the vendor's identifiers verbatim** (`ibkr_conid`,
  the OCC local symbol). It deliberately does not resolve identity across
  sources — that is an open question, and a synthetic key would bury it.
- **`AccountSnapshot.vendor` keeps everything that did not map**, unaltered.
  Reaching into it from a consumer means the canonical schema is missing a
  field, which is a change to make here.
- **Trading212 quotes some lines in a minor unit** (GBX beside GBP, in the same
  account, with no currency in the payload). The scale is recovered from
  `ppl = quantity * (current - average) * factor`, accepted only when it lands
  on a power of ten, and left `None` otherwise — the same failing-closed rule as
  IBKR's multiplier.

## `antlia.fx`

Foreign exchange rates, so figures in different currencies can be added up.

```python
from antlia import fx

table = fx.rates(("USD", "GBP", "JPY", "HKD"))
table.convert(83289.64, "GBP", "USD")  # -> 112753.17
table.source, table.as_of, table.stale  # 'yfinance', ..., False
```

`RateTable.rates` is **units of each currency per 1 USD** — `GBP: 0.7387,
JPY: 160.04` reads as "one dollar buys 0.7387 pounds or 160.04 yen". USD is the
pivot and every conversion crosses through it, so adding a currency is a
one-line change.

```bash
python -m antlia.fx                             # the table, cache or live
python -m antlia.fx --verify                    # every source, live, in turn
python -m antlia.fx --convert 83289.64 GBP USD
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

### A broker's rate stays an account concern

IBKR reports its own rate per currency on `Balance.exchange_rate`, live, and by
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
orientation — units of base per 1 unit of the currency, which is what
`Balance.exchange_rate` carries:

```python
table.inverse("JPY")  # 0.006248 -- the market
snap.balance("JPY").exchange_rate  # 0.006247 -- what IBKR valued the account with
```

Comparing across orientations instead is wrong by a factor of 25,000 and looks
plausible in neither direction, which is why the accessor exists at all.

## Using antlia from another project

```bash
uv add "antlia[ibkr,trading212] @ /path/to/antlia"     # or a git URL
```

Then, from anywhere — credentials live in `~/.antlia/`, so nothing depends on
the working directory:

```python
from antlia import account

legs = [
    p for p in account.snapshot("ibkr", profile="live").positions if p.instrument.kind == "option"
]
```

Four things to know before wiring it in:

- **Python >=3.12**, and ask for the extras you use. A plain `pip install
  antlia` gives you credential resolution and no vendor SDKs at all.
- **Take one `snapshot()` and read from it.** `positions()`, `balances()`,
  `margin()`, `orders()` and `fills()` are each a *full* snapshot underneath —
  four HTTP calls for Trading212 — so calling several in a row multiplies the
  requests and will meet a rate limit that a single snapshot never does.
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

## Development

```bash
uv sync --extra dev
uv run pytest
uv run ruff check . && uv run ruff format --check . && uv run mypy
```

Python 3.12+. The core has no required dependencies — a consumer that wants only
`antlia.auth` is not made to install a query engine.
