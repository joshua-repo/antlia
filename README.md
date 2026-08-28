# antlia

The data layer. *Antlia*, the air pump, moves what is in there out to here
without altering it — this pulls market and account data out of vendor APIs and
makes it available in one shape, and resists improving anything on the way
through.

It is a library. Other projects import it; it is the one place they get data
from, so a backtest, a screen and a dashboard cannot quietly disagree about what
happened.

Four parts, of which the first is built:

| | | |
|---|---|---|
| `auth` | credentials, sessions, rate limits | **built** |
| `account` | live account state, multi-source | **built** (IBKR) |
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
| `trading212` | REST API key | `live`, `demo` | `antlia[trading212]` |
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

## Development

```bash
uv sync --extra dev
uv run pytest
uv run ruff check . && uv run ruff format --check . && uv run mypy
```

Python 3.12+. The core has no required dependencies — a consumer that wants only
`antlia.auth` is not made to install a query engine.
