"""
tests/test_assignment_handler.py
---------------------------------
Unit tests for assignment_handler.py.

Covers:
  - parse_ibkr_codes()           : token splitting
  - classify_assignment_type()   : per-codes-set classification
  - process_assignments()        : end-to-end annotation + A;O numeric fix
  - k4_generator integration     : convert_trade_to_sek() with AssignmentOpen
"""

from datetime import datetime
from pathlib import Path
from unittest.mock import MagicMock

import pytest
import sys

sys.path.insert(0, str(Path(__file__).parent.parent))

from assignment_handler import (
    parse_ibkr_codes,
    classify_assignment_type,
    process_assignments,
)
from models import Trade, K4Trade
from k4_generator import convert_trade_to_sek


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_DATE = datetime(2025, 1, 10, 15, 30)


def _make_trade(
    *,
    symbol: str = "TEST",
    asset_class: str = "EQUITY AND INDEX OPTIONS",
    quantity: float = -1.0,
    proceeds: float = 0.0,
    realized_pnl: float = 0.0,
    ibkr_codes: frozenset = frozenset(),
    currency: str = "USD",
    price: float = 0.0,
    fees: float = 0.0,
) -> Trade:
    return Trade(
        date=_DATE,
        symbol=symbol,
        asset_class=asset_class,
        quantity=quantity,
        price=price,
        proceeds=proceeds,
        fees=fees,
        currency=currency,
        realized_pnl=realized_pnl,
        ibkr_codes=ibkr_codes,
    )


def _fx(rate: float = 10.0) -> MagicMock:
    """Return a mock FXRateProvider that always returns *rate*."""
    provider = MagicMock()
    provider.get_rate.return_value = rate
    provider.prefetch_rates.return_value = None
    return provider


# ---------------------------------------------------------------------------
# parse_ibkr_codes
# ---------------------------------------------------------------------------

class TestParseIbkrCodes:
    def test_single_code(self):
        assert parse_ibkr_codes("O") == frozenset({"O"})

    def test_two_codes(self):
        assert parse_ibkr_codes("A;C") == frozenset({"A", "C"})

    def test_three_codes(self):
        assert parse_ibkr_codes("A;C;P") == frozenset({"A", "C", "P"})

    def test_expiry_code(self):
        assert parse_ibkr_codes("C;Ep") == frozenset({"C", "Ep"})

    def test_empty_string(self):
        assert parse_ibkr_codes("") == frozenset()

    def test_whitespace_only(self):
        assert parse_ibkr_codes("   ") == frozenset()

    def test_whitespace_around_tokens(self):
        assert parse_ibkr_codes(" A ; C ") == frozenset({"A", "C"})

    def test_single_assignment_opening(self):
        assert parse_ibkr_codes("A;O") == frozenset({"A", "O"})

    def test_opening_trade(self):
        assert parse_ibkr_codes("O") == frozenset({"O"})

    def test_closing_trade(self):
        assert parse_ibkr_codes("C") == frozenset({"C"})


# ---------------------------------------------------------------------------
# classify_assignment_type
# ---------------------------------------------------------------------------

class TestClassifyAssignmentType:
    def test_assignment_open_codes(self):
        assert classify_assignment_type(frozenset({"A", "O"})) == "AssignmentOpen"

    def test_assignment_close_codes(self):
        assert classify_assignment_type(frozenset({"A", "C"})) == "Assigned"

    def test_expiry_codes(self):
        assert classify_assignment_type(frozenset({"C", "Ep"})) == "Expired"

    def test_expiry_alone(self):
        # Ep alone is unlikely in practice but should still classify as Expired
        assert classify_assignment_type(frozenset({"Ep"})) == "Expired"

    def test_normal_opening(self):
        assert classify_assignment_type(frozenset({"O"})) is None

    def test_normal_closing(self):
        assert classify_assignment_type(frozenset({"C"})) is None

    def test_partial_closing(self):
        assert classify_assignment_type(frozenset({"C", "P"})) is None

    def test_empty_codes(self):
        assert classify_assignment_type(frozenset()) is None

    def test_assignment_open_priority_over_expiry(self):
        # A;O should be classified as AssignmentOpen, not Expired
        assert classify_assignment_type(frozenset({"A", "O", "Ep"})) == "AssignmentOpen"

    def test_assignment_close_three_codes(self):
        # A;C;P (closing with partial fill) → still Assigned
        assert classify_assignment_type(frozenset({"A", "C", "P"})) == "Assigned"


# ---------------------------------------------------------------------------
# process_assignments — annotation
# ---------------------------------------------------------------------------

class TestProcessAssignments:
    def test_returns_same_count(self):
        trades = [
            _make_trade(ibkr_codes=frozenset({"O"})),
            _make_trade(ibkr_codes=frozenset({"C"})),
            _make_trade(ibkr_codes=frozenset({"C", "Ep"})),
        ]
        result = process_assignments(trades)
        assert len(result) == 3

    def test_normal_opening_no_type(self):
        trade = _make_trade(ibkr_codes=frozenset({"O"}))
        result = process_assignments([trade])
        assert result[0].assignment_type is None

    def test_normal_closing_no_type(self):
        trade = _make_trade(ibkr_codes=frozenset({"C"}))
        result = process_assignments([trade])
        assert result[0].assignment_type is None

    def test_expired_option_annotated(self):
        """C;Ep row → assignment_type = 'Expired'."""
        trade = _make_trade(
            symbol="SPX 4500 C",
            ibkr_codes=frozenset({"C", "Ep"}),
            proceeds=0.0,
            realized_pnl=350.0,  # short option: premium kept
        )
        result = process_assignments([trade])
        assert result[0].assignment_type == "Expired"

    def test_assigned_option_leg_annotated(self):
        """A;C row (option side consumed) → assignment_type = 'Assigned'."""
        trade = _make_trade(
            symbol="QQQ 510 P",
            asset_class="EQUITY AND INDEX OPTIONS",
            ibkr_codes=frozenset({"A", "C"}),
            proceeds=0.0,
            realized_pnl=0.0,
        )
        result = process_assignments([trade])
        assert result[0].assignment_type == "Assigned"

    def test_assignment_open_stock_annotated(self):
        """A;O row (stock purchased via assignment) → assignment_type = 'AssignmentOpen'."""
        trade = _make_trade(
            symbol="QQQ",
            asset_class="STOCKS",
            ibkr_codes=frozenset({"A", "O"}),
            quantity=100.0,
            proceeds=-51000.0,  # debit encoded as negative proceeds
            realized_pnl=0.0,
        )
        result = process_assignments([trade])
        assert result[0].assignment_type == "AssignmentOpen"

    def test_preserves_other_fields(self):
        """process_assignments must not alter any fields other than assignment_type."""
        trade = _make_trade(
            symbol="QQQ",
            asset_class="STOCKS",
            ibkr_codes=frozenset({"A", "O"}),
            proceeds=-51000.0,
            realized_pnl=0.0,
        )
        result = process_assignments([trade])
        r = result[0]
        assert r.symbol == "QQQ"
        assert r.asset_class == "STOCKS"
        assert r.proceeds == -51000.0
        assert r.realized_pnl == 0.0
        assert r.ibkr_codes == frozenset({"A", "O"})

    def test_empty_list(self):
        assert process_assignments([]) == []

    def test_mixed_list_annotates_correctly(self):
        """All three event types in one call."""
        trades = [
            _make_trade(symbol="OPT_EXPIRE", ibkr_codes=frozenset({"C", "Ep"})),
            _make_trade(symbol="OPT_ASSIGNED", ibkr_codes=frozenset({"A", "C"})),
            _make_trade(symbol="STK_RECEIVED", ibkr_codes=frozenset({"A", "O"}), proceeds=-51000.0),
            _make_trade(symbol="NORMAL", ibkr_codes=frozenset({"C"})),
        ]
        result = process_assignments(trades)
        assert result[0].assignment_type == "Expired"
        assert result[1].assignment_type == "Assigned"
        assert result[2].assignment_type == "AssignmentOpen"
        assert result[3].assignment_type is None


# ---------------------------------------------------------------------------
# process_assignments — equity option assignment scenario
# (short put on QQQ, real CSV pattern)
# ---------------------------------------------------------------------------

class TestShortPutAssignmentEquity:
    """
    Simulate a short QQQ 510 put being assigned:
      - Option leg: A;C, proceeds=0, pnl=0
      - Stock leg:  A;O, proceeds=-51000 (debit), pnl=0
    """

    def _build_trades(self):
        option_leg = _make_trade(
            symbol="QQQ 10JAN25 510 P",
            asset_class="EQUITY AND INDEX OPTIONS",
            quantity=-1.0,
            ibkr_codes=frozenset({"A", "C"}),
            proceeds=0.0,
            realized_pnl=0.0,
        )
        stock_leg = _make_trade(
            symbol="QQQ",
            asset_class="STOCKS",
            quantity=100.0,
            ibkr_codes=frozenset({"A", "O"}),
            proceeds=-51000.0,
            realized_pnl=0.0,
        )
        return [option_leg, stock_leg]

    def test_annotation(self):
        result = process_assignments(self._build_trades())
        assert result[0].assignment_type == "Assigned"
        assert result[1].assignment_type == "AssignmentOpen"

    def test_k4_option_leg_zero_pnl(self):
        """Option leg: A;C → proceeds=0, pnl=0 → K4 amounts all zero."""
        result = process_assignments(self._build_trades())
        k4 = convert_trade_to_sek(result[0], _fx(rate=10.5))
        assert k4.sale_amount_sek == 0.0
        assert k4.purchase_amount_sek == 0.0
        assert k4.profit_loss_sek == 0.0

    def test_k4_stock_leg_positive_purchase(self):
        """Stock leg: A;O → purchase_amount = abs(-51000)*rate, sale_amount=0."""
        result = process_assignments(self._build_trades())
        rate = 10.5
        k4 = convert_trade_to_sek(result[1], _fx(rate=rate))
        assert k4.sale_amount_sek == 0.0
        assert k4.purchase_amount_sek == pytest.approx(51000 * rate)
        assert k4.profit_loss_sek == 0.0

    def test_k4_stock_leg_no_negative_amounts(self):
        """A;O rows must NEVER produce negative sale or purchase amounts."""
        result = process_assignments(self._build_trades())
        k4 = convert_trade_to_sek(result[1], _fx(rate=10.5))
        assert k4.sale_amount_sek >= 0.0
        assert k4.purchase_amount_sek >= 0.0

    def test_k4_stock_leg_assignment_type_propagated(self):
        result = process_assignments(self._build_trades())
        k4 = convert_trade_to_sek(result[1], _fx(rate=10.5))
        assert k4.assignment_type == "AssignmentOpen"


# ---------------------------------------------------------------------------
# process_assignments — expired options scenario
# ---------------------------------------------------------------------------

class TestOptionExpiry:
    """
    C;Ep rows: proceeds=0, realized_pnl carries the full P&L.
    Short option (credit received): pnl > 0
    Long option (debit paid):       pnl < 0
    The existing formula handles these correctly; we test the annotation and
    that the numeric result is unaffected.
    """

    def test_short_option_expired_worthless(self):
        """Short option expires worthless: premium = 350 USD kept as profit."""
        trade = _make_trade(
            symbol="SPX 18APR25 4500 C",
            asset_class="EQUITY AND INDEX OPTIONS",
            quantity=-1.0,
            ibkr_codes=frozenset({"C", "Ep"}),
            proceeds=0.0,
            realized_pnl=350.0,
        )
        rate = 10.0
        [annotated] = process_assignments([trade])
        assert annotated.assignment_type == "Expired"

        k4 = convert_trade_to_sek(annotated, _fx(rate=rate))
        # proceeds=0 → sale_amount=0
        assert k4.sale_amount_sek == 0.0
        # cost_basis = 0 - 350 = -350  → purchase_amount_sek = -3500
        assert k4.purchase_amount_sek == pytest.approx(-350 * rate)
        # pnl = 0 - (-350*rate) = +3500
        assert k4.profit_loss_sek == pytest.approx(350 * rate)

    def test_long_option_expired_worthless(self):
        """Long option expires worthless: full premium paid is the loss."""
        trade = _make_trade(
            symbol="AAPL 21MAR25 180 P",
            asset_class="EQUITY AND INDEX OPTIONS",
            quantity=1.0,
            ibkr_codes=frozenset({"C", "Ep"}),
            proceeds=0.0,
            realized_pnl=-200.0,
        )
        rate = 10.0
        [annotated] = process_assignments([trade])
        assert annotated.assignment_type == "Expired"

        k4 = convert_trade_to_sek(annotated, _fx(rate=rate))
        assert k4.sale_amount_sek == 0.0
        # cost_basis = 0 - (-200) = 200 → purchase_amount_sek = 2000
        assert k4.purchase_amount_sek == pytest.approx(200 * rate)
        # pnl = 0 - 200*rate = -2000
        assert k4.profit_loss_sek == pytest.approx(-200 * rate)


# ---------------------------------------------------------------------------
# process_assignments — options-on-futures assignment scenario
# ---------------------------------------------------------------------------

class TestOptionsOnFuturesAssignment:
    """
    Options-on-futures (FOP) assigned: both legs use A;C code.
    Example: MES 02JUN25 5945 C option + MESM5 futures contract.
    """

    def _build_trades(self):
        option_leg = _make_trade(
            symbol="MES 02JUN25 5945 C",
            asset_class="OPTIONS ON FUTURES",
            quantity=-1.0,
            ibkr_codes=frozenset({"A", "C"}),
            proceeds=0.0,
            realized_pnl=0.0,
        )
        futures_leg = _make_trade(
            symbol="MESM5",
            asset_class="FUTURES",
            quantity=1.0,
            ibkr_codes=frozenset({"A", "C"}),
            proceeds=59450.0,
            realized_pnl=512.88,
        )
        return [option_leg, futures_leg]

    def test_both_legs_annotated_as_assigned(self):
        result = process_assignments(self._build_trades())
        assert result[0].assignment_type == "Assigned"
        assert result[1].assignment_type == "Assigned"

    def test_futures_leg_pnl_preserved(self):
        """Futures leg P&L must flow through correctly (not treated as AssignmentOpen)."""
        result = process_assignments(self._build_trades())
        rate = 11.0
        k4 = convert_trade_to_sek(result[1], _fx(rate=rate))
        # Normal formula: sale = proceeds*rate, cost = (proceeds-pnl)*rate
        expected_sale = 59450.0 * rate
        expected_cost = (59450.0 - 512.88) * rate
        expected_pnl = 512.88 * rate
        assert k4.sale_amount_sek == pytest.approx(expected_sale, rel=1e-6)
        assert k4.purchase_amount_sek == pytest.approx(expected_cost, rel=1e-6)
        assert k4.profit_loss_sek == pytest.approx(expected_pnl, rel=1e-6)


# ---------------------------------------------------------------------------
# K4Trade assignment_type field propagation
# ---------------------------------------------------------------------------

class TestK4TradeAssignmentTypePropagation:
    def test_normal_trade_no_assignment_type(self):
        trade = _make_trade(
            symbol="AAPL",
            asset_class="STOCKS",
            proceeds=10000.0,
            realized_pnl=500.0,
        )
        k4 = convert_trade_to_sek(trade, _fx(rate=10.0))
        assert k4.assignment_type is None

    def test_expired_propagated(self):
        trade = _make_trade(
            ibkr_codes=frozenset({"C", "Ep"}),
            proceeds=0.0,
            realized_pnl=100.0,
        )
        [annotated] = process_assignments([trade])
        k4 = convert_trade_to_sek(annotated, _fx(rate=10.0))
        assert k4.assignment_type == "Expired"

    def test_assigned_propagated(self):
        trade = _make_trade(
            ibkr_codes=frozenset({"A", "C"}),
            proceeds=0.0,
            realized_pnl=0.0,
        )
        [annotated] = process_assignments([trade])
        k4 = convert_trade_to_sek(annotated, _fx(rate=10.0))
        assert k4.assignment_type == "Assigned"

    def test_assignment_open_propagated(self):
        trade = _make_trade(
            symbol="QQQ",
            asset_class="STOCKS",
            ibkr_codes=frozenset({"A", "O"}),
            proceeds=-51000.0,
            realized_pnl=0.0,
        )
        [annotated] = process_assignments([trade])
        k4 = convert_trade_to_sek(annotated, _fx(rate=10.0))
        assert k4.assignment_type == "AssignmentOpen"
