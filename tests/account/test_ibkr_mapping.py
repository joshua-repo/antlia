"""The normalisation decisions, tested against the shapes a live gateway sent."""

from __future__ import annotations

from datetime import date

import pytest

from antlia.account.sources.ibkr import IBKRAccounts, instrument_of, parse_expiry, sane
from antlia.account.types import BASE, OPTION, OTHER, STOCK
from tests.account import fakes


class TestInstrument:
    def test_option_carries_expiry_strike_right_and_multiplier(self):
        i = instrument_of(fakes.option(strike=100.0, right="P"))
        assert i.kind == OPTION
        assert i.expiry == date(2026, 9, 11)
        assert i.strike == 100.0
        assert i.right == "P"
        assert i.multiplier == 100.0

    def test_stock_has_no_option_fields_and_multiplier_one(self):
        i = instrument_of(fakes.stock())
        assert i.kind == STOCK
        assert (i.expiry, i.strike, i.right) == (None, None, None)
        # IBKR sends right='0' and multiplier='' for equities; neither is data.
        assert i.multiplier == 1.0

    def test_vendor_ids_are_preserved_verbatim(self):
        # Not resolved into a synthetic key: cross-source identity is an open
        # question, and burying it would make it unanswerable later.
        i = instrument_of(fakes.option(conid=907051546))
        assert i.ids["ibkr_conid"] == "907051546"
        assert i.ids["ibkr_local_symbol"].startswith("AAOI")

    def test_unknown_sectype_is_other_not_a_crash(self):
        assert instrument_of(fakes.Contract(secType="WAR")).kind == OTHER

    @pytest.mark.parametrize(
        ("raw", "expected"),
        [("20260911", date(2026, 9, 11)), ("202609", date(2026, 9, 1)), ("", None), ("x", None)],
    )
    def test_expiry_handles_both_ibkr_formats(self, raw, expected):
        assert parse_expiry(raw) == expected


class TestSane:
    def test_drops_the_unset_sentinel(self):
        assert sane(1.7976931348623157e308) is None

    def test_drops_nan_but_keeps_a_real_zero(self):
        assert sane(float("nan")) is None
        assert sane(0.0) == 0.0

    def test_reads_the_strings_ibkr_sends(self):
        assert sane("57752.50") == 57752.5
        assert sane("") is None
        assert sane("INDIVIDUAL") is None


class TestPositions:
    source = IBKRAccounts()

    def option_item(self):
        return fakes.PortfolioItem(
            contract=fakes.option(),
            position=1.0,
            marketPrice=3.50022815,
            marketValue=350.02,
            averageCost=290.0403,
            unrealizedPNL=59.98,
            realizedPNL=0.0,
        )

    def test_average_price_is_put_on_the_same_scale_as_market_price(self):
        # The whole point: IBKR's avgCost includes the multiplier and
        # marketPrice does not, so 2.90 arrives as 290.04 beside 3.50.
        ib = fakes.FakeIB(portfolio=[self.option_item()])
        (pos,) = self.source._positions(ib, "U1", _stamp())
        assert pos.average_price is not None and pos.market_price is not None
        assert pos.average_price == pytest.approx(2.900403)
        assert pos.market_price == pytest.approx(3.50022815)
        assert pos.average_price < pos.market_price

    def test_cost_basis_puts_the_multiplier_back(self):
        ib = fakes.FakeIB(portfolio=[self.option_item()])
        (pos,) = self.source._positions(ib, "U1", _stamp())
        assert pos.cost_basis == pytest.approx(290.0403)

    def test_equity_average_price_is_untouched(self):
        item = fakes.PortfolioItem(
            contract=fakes.stock(),
            position=100.0,
            marketPrice=4797.2001953,
            marketValue=479720.02,
            averageCost=5262.79902,
            unrealizedPNL=-46559.88,
            realizedPNL=0.0,
        )
        (pos,) = self.source._positions(fakes.FakeIB(portfolio=[item]), "U1", _stamp())
        assert pos.average_price == pytest.approx(5262.79902)
        assert pos.cost_basis == pytest.approx(526279.902)

    def test_short_positions_keep_their_sign(self):
        item = self.option_item()
        item.position = -1.0
        item.marketValue = -1575.71
        (pos,) = self.source._positions(fakes.FakeIB(portfolio=[item]), "U1", _stamp())
        assert pos.is_short
        assert pos.cost_basis is not None and pos.cost_basis < 0

    def test_other_accounts_are_filtered_out(self):
        mine, theirs = self.option_item(), self.option_item()
        theirs.account = "U2"
        ib = fakes.FakeIB(portfolio=[mine, theirs])
        assert len(self.source._positions(ib, "U1", _stamp())) == 1


class TestBaseCurrency:
    source = IBKRAccounts()

    def test_read_off_net_liquidation_not_the_currency_tag(self):
        # The Currency tag appears once per currency held; picking one gives
        # whichever arrived first, which mislabels every margin figure.
        assert self.source._base_currency(fakes.values_like_a_live_account()) == "USD"

    def test_falls_back_to_the_currency_whose_rate_is_one(self):
        values = [v for v in fakes.values_like_a_live_account() if v.tag != "NetLiquidation"]
        assert self.source._base_currency(values) == "USD"

    def test_a_single_currency_account_still_resolves(self):
        values = [fakes.AccountValue("NetLiquidation", "1000", "GBP")]
        assert self.source._base_currency(values) == "GBP"

    def test_nothing_to_go_on_yields_base(self):
        assert self.source._base_currency([]) == BASE


class TestBalancesAndMargin:
    source = IBKRAccounts()

    def test_one_balance_per_currency_including_the_rollup(self):
        balances = self.source._balances(fakes.values_like_a_live_account(), "U1", _stamp())
        assert {b.currency for b in balances} == {"BASE", "JPY", "USD"}
        rollup = next(b for b in balances if b.currency == BASE)
        assert rollup.is_consolidated
        assert not next(b for b in balances if b.currency == "USD").is_consolidated

    def test_per_currency_values_do_not_bleed_into_each_other(self):
        balances = {
            b.currency: b
            for b in self.source._balances(fakes.values_like_a_live_account(), "U1", _stamp())
        }
        assert balances["JPY"].cash == pytest.approx(-2304417.40)
        assert balances["USD"].cash == pytest.approx(3008.7613)
        assert balances["JPY"].exchange_rate == pytest.approx(0.0062559)

    def test_margin_is_denominated_in_the_base_currency(self):
        values = fakes.values_like_a_live_account()
        margin = self.source._margin(values, "U1", "USD", _stamp())
        assert margin.currency == "USD"
        assert margin.net_liquidation == pytest.approx(85035.05)
        assert margin.maintenance_margin == pytest.approx(57752.50)
        assert margin.excess_liquidity == pytest.approx(33846.67)

    def test_dimensionless_tags_still_resolve(self):
        margin = self.source._margin(fakes.values_like_a_live_account(), "U1", "USD", _stamp())
        assert margin.cushion == pytest.approx(0.398032)
        assert margin.leverage == pytest.approx(1.60)

    def test_an_unreadable_tag_is_none_never_zero(self):
        # DayTradesRemaining arrives empty. A zero here reads as "no day trades
        # left", which is a different and alarming claim.
        margin = self.source._margin(fakes.values_like_a_live_account(), "U1", "USD", _stamp())
        assert margin.day_trades_remaining is None

    def test_utilisation(self):
        margin = self.source._margin(fakes.values_like_a_live_account(), "U1", "USD", _stamp())
        assert margin.utilisation == pytest.approx(57752.50 / 85035.05)


class TestOrdersAndFills:
    source = IBKRAccounts()

    def test_unset_stop_price_becomes_none(self):
        ib = fakes.FakeIB(trades=[fakes.Trade(contract=fakes.option())])
        (order,) = self.source._orders(ib, "U1", _stamp())
        assert order.limit_price == pytest.approx(1.25)
        assert order.stop_price is None  # IBKR's 1.79e308 is not a price
        assert order.side == "buy"
        assert order.ids["ibkr_perm_id"] == "99"

    def test_fill_sides_are_normalised(self):
        bought = fakes.FillRecord(contract=fakes.option())
        sold = fakes.FillRecord(contract=fakes.option())
        sold.execution = fakes.Execution(side="SLD", execId="x")
        fills = self.source._fills(fakes.FakeIB(fills=[bought, sold]), "U1")
        assert [f.side for f in fills] == ["buy", "sell"]

    def test_fill_carries_commission_and_realized_pnl(self):
        (fill,) = self.source._fills(
            fakes.FakeIB(fills=[fakes.FillRecord(contract=fakes.option())]), "U1"
        )
        assert fill.commission == pytest.approx(1.04028)
        assert fill.commission_currency == "USD"
        assert fill.realized_pnl == pytest.approx(782.88712)


class TestSnapshot:
    def test_composes_everything_and_never_calls_req_account_updates(self):
        ib = fakes.FakeIB(
            values=fakes.values_like_a_live_account(),
            portfolio=[
                fakes.PortfolioItem(fakes.option(), 1.0, 3.5, 350.02, 290.04, 59.98, 0.0),
                fakes.PortfolioItem(
                    fakes.stock(), 100.0, 4797.2, 479720.02, 5262.8, -46559.88, 0.0
                ),
            ],
            fills=[fakes.FillRecord(contract=fakes.option())],
        )
        source = IBKRAccounts()
        source._session = lambda profile, overrides: _ctx(ib)  # type: ignore[method-assign]

        snap = source.snapshot()
        assert snap.account == "U1"
        assert snap.base_currency == "USD"
        assert len(snap.positions) == 2
        assert len(snap.by_kind(OPTION)) == 1
        jpy = snap.balance("JPY")
        assert jpy is not None
        assert jpy.cash == pytest.approx(-2304417.40)
        assert snap.net_liquidation() == pytest.approx(85035.05)
        # Everything the vendor said is kept, unaltered, beside the mapping.
        assert snap.vendor["account_values"]["NetLiquidation:USD"] == "85035.05"

    def test_settle_waits_for_the_first_push_then_stops(self):
        source = IBKRAccounts()
        empty = fakes.FakeIB(values=[])
        source._settle(empty)
        assert empty.slept == pytest.approx(source.settle_timeout)

        ready = fakes.FakeIB(values=fakes.values_like_a_live_account())
        source._settle(ready)
        assert ready.slept == 0.0


def _stamp():
    from antlia.account.base import now

    return now()


class _ctx:
    def __init__(self, ib):
        self.ib = ib

    def __enter__(self):
        return self.ib

    def __exit__(self, *exc):
        return False
