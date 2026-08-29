"""Trading212 mapping, against payload shapes observed on a live account.

The fixtures below are real response shapes with the amounts kept -- the
GBP/GBX mixture in particular is not hypothetical, and it is the thing most
likely to be broken by a well-meaning simplification.
"""

from __future__ import annotations

from typing import Any

import pytest

from antlia.account.sources.trading212 import (
    Trading212Accounts,
    instrument_of,
    parse_time,
    price_factor,
)
from antlia.account.types import STOCK
from antlia.auth.errors import ConnectionFailed

SUMMARY: dict[str, Any] = {
    "id": 31944381,
    "currency": "GBP",
    "totalValue": 83289.61,
    "cash": {"availableToTrade": 322.26, "reservedForOrders": 0, "inPies": 0},
    "investments": {
        "currentValue": 82967.35,
        "totalCost": 71799.06,
        "realizedProfitLoss": 874.21,
        "unrealizedProfitLoss": 11168.29,
    },
}

#: FRXTl is quoted in GBP; the rest in GBX. Same account, same ticker suffix.
PORTFOLIO: list[dict[str, Any]] = [
    {
        "ticker": "FRXTl_EQ",
        "quantity": 200.0,
        "averagePrice": 51.9121,
        "currentPrice": 53.2,
        "ppl": 257.57,
        "fxPpl": None,
        "pieQuantity": 0,
    },
    {
        "ticker": "GSKl_EQ",
        "quantity": 200.0,
        "averagePrice": 1875.0,
        "currentPrice": 1853.5,
        "ppl": -43.0,
        "fxPpl": None,
        "pieQuantity": 0,
    },
    {
        "ticker": "HSBAl_EQ",
        "quantity": 400.0,
        "averagePrice": 1229.9,
        "currentPrice": 1528.4,
        "ppl": 1194.0,
        "fxPpl": None,
        "pieQuantity": 0,
    },
    {
        "ticker": "SHELl_EQ",
        "quantity": 150.0,
        "averagePrice": 3119.6667,
        "currentPrice": 3344.5,
        "ppl": 337.25,
        "fxPpl": None,
        "pieQuantity": 0,
    },
    {
        "ticker": "SPXPl_EQ",
        "quantity": 5000.0,
        "averagePrice": 961.3508,
        "currentPrice": 1149.8,
        "ppl": 9422.47,
        "fxPpl": None,
        "pieQuantity": 0,
    },
]

HISTORY: dict[str, Any] = {
    "items": [
        {
            "order": {
                "id": 53105971181,
                "type": "LIMIT",
                "ticker": "SHELl_EQ",
                "quantity": 50.0,
                "filledQuantity": 50.0,
                "limitPrice": 2999.0,
                "status": "FILLED",
                "currency": "GBP",
                "side": "BUY",
                "createdAt": "2026-06-23T13:12:09.000Z",
                "instrument": {
                    "ticker": "SHELl_EQ",
                    "name": "Shell",
                    "isin": "GB00BP6MXD84",
                    "currency": "GBX",
                },
            },
            "fill": {
                "id": 991,
                "quantity": 50.0,
                "price": 2999.0,
                "filledAt": "2026-06-23T13:12:53.000Z",
                "walletImpact": {
                    "currency": "GBP",
                    "netValue": -1507.0,
                    "fxRate": 1,
                    "taxes": [{"name": "STAMP_DUTY", "quantity": -7.5}],
                },
            },
        },
        {
            "order": {
                "id": 53105971182,
                "ticker": "GSKl_EQ",
                "side": "SELL",
                "createdAt": "2026-06-22T15:08:00.000Z",
                "instrument": {
                    "ticker": "GSKl_EQ",
                    "name": "GSK",
                    "isin": "GB00BN7SWP63",
                    "currency": "GBX",
                },
            },
            # T212 signs a sale's quantity; the canonical schema puts the sign
            # in `side` instead.
            "fill": {
                "id": 992,
                "quantity": -100.0,
                "price": 1926.0,
                "filledAt": "2026-06-22T15:08:33.000Z",
                "walletImpact": {},
            },
        },
    ],
    "nextPagePath": None,
}


class Response:
    def __init__(self, payload, status=200, headers=None):
        self._payload = payload
        self.status_code = status
        self.text = "boom"
        self.headers = headers or {}

    def json(self):
        return self._payload


class Client:
    def __init__(self, routes=None, status=200, forbid=()):
        self.routes = routes or {
            "/api/v0/equity/account/summary": SUMMARY,
            "/api/v0/equity/portfolio": PORTFOLIO,
            "/api/v0/equity/orders": [],
            "/api/v0/equity/history/orders": HISTORY,
        }
        self.status = status
        self.forbid = set(forbid)
        self.calls: list[str] = []

    def get(self, path, params=None):
        self.calls.append(path)
        if path in self.forbid:
            return Response({}, status=403)
        return Response(self.routes.get(path, {}), status=self.status)


@pytest.fixture
def source():
    src = Trading212Accounts()
    src._session = lambda profile, overrides: _ctx(Client())  # type: ignore[method-assign]
    return src


class _ctx:
    def __init__(self, client):
        self.client = client

    def __enter__(self):
        return self.client

    def __exit__(self, *exc):
        return False


class TestPriceFactor:
    """Recovering GBP-vs-GBX from two numbers the vendor already reported."""

    def test_a_major_unit_line_needs_no_scaling(self):
        assert price_factor(200.0, 51.9121, 53.2, 257.57) == pytest.approx(1.0)

    def test_a_pence_quoted_line_is_scaled_down(self):
        assert price_factor(200.0, 1875.0, 1853.5, -43.0) == pytest.approx(0.01)

    def test_an_unmoved_position_gives_nothing_rather_than_a_guess(self):
        # ppl / 0 is undefined; a fabricated factor here would be a silent 100x.
        assert price_factor(200.0, 100.0, 100.0, 0.0) is None

    def test_an_implausible_ratio_is_refused(self):
        # Not near any power of ten -- something other than unit scaling is in
        # play (an FX leg, a corporate action), so decline.
        assert price_factor(100.0, 10.0, 12.0, 137.0) is None

    @pytest.mark.parametrize("bad", [None, 0.0])
    def test_missing_inputs_decline(self, bad):
        assert price_factor(bad, 10.0, 12.0, 200.0) is None
        assert price_factor(100.0, 10.0, 12.0, bad if bad is None else None) is None


class TestInstrument:
    def test_the_symbol_keeps_its_venue_letter(self):
        # `GSKl` is not cleaned to `GSK`: guessing the suffix off would merge
        # two listings, and cross-source identity is an open question.
        assert instrument_of("GSKl_EQ").symbol == "GSKl"
        assert instrument_of("AAPL_US_EQ").symbol == "AAPL"

    def test_the_isin_is_kept_when_the_payload_has_one(self):
        i = instrument_of("SHELl_EQ", {"isin": "GB00BP6MXD84", "currency": "GBX"})
        assert i.ids["isin"] == "GB00BP6MXD84"
        assert i.ids["trading212_ticker"] == "SHELl_EQ"
        assert i.currency == "GBX"

    def test_everything_is_equity(self):
        assert instrument_of("AAPL_US_EQ").kind == STOCK

    def test_a_portfolio_row_has_no_currency_to_offer(self):
        assert instrument_of("GSKl_EQ").currency == ""


class TestSnapshot:
    def test_market_value_is_scaled_per_position_not_per_account(self, source):
        # The whole point of price_factor: one GBP line beside two GBX ones.
        values = {p.instrument.symbol: p.market_value for p in source.snapshot().positions}
        assert values["FRXTl"] == pytest.approx(10640.00)
        assert values["GSKl"] == pytest.approx(3707.00)
        assert values["SPXPl"] == pytest.approx(57490.00)

    def test_the_scaled_values_reconcile_with_the_vendor_total(self, source):
        # Independent cross-check: T212 reports investments.currentValue itself.
        snap = source.snapshot()
        total = sum(p.market_value or 0.0 for p in snap.positions)
        assert total == pytest.approx(SUMMARY["investments"]["currentValue"], abs=0.5)

    def test_prices_stay_in_the_instruments_own_units(self, source):
        gsk = next(p for p in source.snapshot().positions if p.instrument.symbol == "GSKl")
        assert gsk.average_price == pytest.approx(1875.0)
        assert gsk.market_price == pytest.approx(1853.5)
        assert gsk.unrealized_pnl == pytest.approx(-43.0)

    def test_balance_comes_from_summary(self, source):
        (balance,) = source.snapshot().balances
        assert balance.currency == "GBP"
        assert balance.cash == pytest.approx(322.26)
        assert balance.net_liquidation == pytest.approx(83289.61)
        assert balance.realized_pnl == pytest.approx(874.21)

    def test_account_and_currency_come_from_summary_not_info(self, source):
        # account/info is one call per 30s; summary is one per 5s and carries
        # the same two fields plus the totals.
        snap = source.snapshot()
        assert snap.account == "31944381"
        assert snap.base_currency == "GBP"
        assert "/api/v0/equity/account/info" not in _calls(source)


class TestFills:
    def test_fills_come_from_order_history(self, source):
        fills = source.snapshot().fills
        assert len(fills) == 2

    def test_the_sign_moves_from_quantity_to_side(self, source):
        sell = next(f for f in source.snapshot().fills if f.side == "sell")
        assert sell.quantity == pytest.approx(100.0)
        assert sell.instrument.symbol == "GSKl"

    def test_commission_is_a_positive_cost(self, source):
        # walletImpact taxes arrive as signed debits.
        buy = next(f for f in source.snapshot().fills if f.side == "buy")
        assert buy.commission == pytest.approx(7.5)
        assert buy.commission_currency == "GBP"

    def test_fills_carry_the_isin_the_portfolio_lacks(self, source):
        buy = next(f for f in source.snapshot().fills if f.side == "buy")
        assert buy.ids["trading212_order_id"] == "53105971181"
        assert buy.instrument.ids["isin"] == "GB00BP6MXD84"

    def test_timestamps_are_timezone_aware(self, source):
        assert all(f.time.tzinfo is not None for f in source.snapshot().fills)

    @pytest.mark.parametrize("raw", ["2026-06-23T13:12:09.000Z", "2026-05-18T17:06:17.000+03:00"])
    def test_both_offset_spellings_parse(self, raw):
        assert parse_time(raw) is not None

    def test_unparseable_or_absent_times_yield_none(self):
        assert parse_time("") is None
        assert parse_time("not a time") is None


class TestDegradation:
    def test_a_forbidden_endpoint_does_not_sink_the_snapshot(self):
        # No trading scope on the key is the correct posture for antlia, so a
        # 403 on working orders must cost only the orders.
        source = Trading212Accounts()
        client = Client(forbid={"/api/v0/equity/orders"})
        source._session = lambda profile, overrides: _ctx(client)  # type: ignore[method-assign]
        snap = source.snapshot()
        assert snap.orders == ()
        assert snap.positions
        assert any("not permitted" in note for note in snap.vendor["unavailable"])

    def test_no_margin_on_a_cash_account(self, source):
        assert source.snapshot().margin is None

    @pytest.mark.parametrize(
        ("status", "message"),
        [(401, "key/secret pair"), (403, "scope"), (500, "HTTP 500")],
    )
    def test_each_refusal_says_what_to_do(self, status, message):
        with pytest.raises(ConnectionFailed, match=message):
            Trading212Accounts()._get(Client(status=status), "/api/v0/equity/portfolio")

    def test_a_429_is_retried_once_at_the_stated_reset(self, monkeypatch):
        # Patched by string so the clock stays real everywhere else; monkeypatch
        # restores it either way.
        slept: list[float] = []
        monkeypatch.setattr("time.sleep", slept.append)
        monkeypatch.setattr("time.time", lambda: 1000.0)

        client = Client()
        answers = [
            Response({}, status=429, headers={"x-ratelimit-reset": "1003"}),
            Response(PORTFOLIO),
        ]
        client.get = lambda path, params=None: answers.pop(0)  # type: ignore[method-assign]
        assert Trading212Accounts()._get(client, "/api/v0/equity/portfolio") == PORTFOLIO
        assert slept == [3.0]

    def test_a_reset_further_out_than_any_window_is_not_waited_on(self, monkeypatch):
        # An epoch far in the future means the header is not what we think it
        # is; sleeping on it would hang the caller for an unbounded time.
        monkeypatch.setattr("time.time", lambda: 1000.0)
        client = Client()
        client.get = lambda path, params=None: Response(  # type: ignore[method-assign]
            {}, status=429, headers={"x-ratelimit-reset": "999999"}
        )
        with pytest.raises(ConnectionFailed, match="429"):
            Trading212Accounts()._get(client, "/api/v0/equity/portfolio")

    def test_a_second_429_is_not_retried_again(self, monkeypatch):
        monkeypatch.setattr("time.sleep", lambda s: None)
        monkeypatch.setattr("time.time", lambda: 1000.0)
        client = Client()
        calls: list[str] = []

        def get(path, params=None):
            calls.append(path)
            return Response({}, status=429, headers={"x-ratelimit-reset": "1002"})

        client.get = get  # type: ignore[method-assign]
        with pytest.raises(ConnectionFailed, match="429"):
            Trading212Accounts()._get(client, "/api/v0/equity/portfolio")
        assert len(calls) == 2

    def test_a_429_quotes_the_endpoints_own_budget(self):
        client = Client()
        client.get = lambda path, params=None: Response(  # type: ignore[method-assign]
            {},
            status=429,
            headers={
                "x-ratelimit-limit": "1",
                "x-ratelimit-period": "30",
                "x-ratelimit-reset": "1787960935",
            },
        )
        with pytest.raises(ConnectionFailed, match="1 per 30s"):
            Trading212Accounts()._get(client, "/api/v0/equity/account/info")


def _calls(source):
    client = Client()
    source._session = lambda profile, overrides: _ctx(client)
    source.snapshot()
    return client.calls


def test_snapshot_calls_each_endpoint_at_most_once(source):
    # Every endpoint has its own budget; an accidental repeat is a 429.
    calls = _calls(source)
    assert len(calls) == len(set(calls))
