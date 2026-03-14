"""
tests/test_reconciliation.py
-----------------------------
Unit tests for reconciliation.py.
"""

from datetime import datetime
from pathlib import Path

import pytest

import sys
sys.path.insert(0, str(Path(__file__).parent.parent))

from models import K4Trade
from reconciliation import (
    ControlDiff,
    DerivativeBreakdown,
    ReconciliationResult,
    SectionTotals,
    compute_reconciliation,
    format_report,
    write_report,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_trade(
    *,
    asset_class: str,
    k4_section: str,
    sale_amount_sek: float,
    purchase_amount_sek: float,
    profit_loss_sek: float,
    symbol: str = "TEST",
    date: datetime | None = None,
    quantity: float = 1.0,
) -> K4Trade:
    return K4Trade(
        date=date or datetime(2025, 3, 15, 10, 0),
        symbol=symbol,
        asset_class=asset_class,
        quantity=quantity,
        sale_amount_sek=sale_amount_sek,
        purchase_amount_sek=purchase_amount_sek,
        profit_loss_sek=profit_loss_sek,
        k4_section=k4_section,
    )


def _stock_trade(sale=1000.0, cost=900.0, pnl=100.0) -> K4Trade:
    return _make_trade(
        asset_class="STOCKS",
        k4_section="A",
        sale_amount_sek=sale,
        purchase_amount_sek=cost,
        profit_loss_sek=pnl,
    )


def _futures_trade(sale=500.0, cost=450.0, pnl=50.0) -> K4Trade:
    return _make_trade(
        asset_class="FUTURES",
        k4_section="D",
        sale_amount_sek=sale,
        purchase_amount_sek=cost,
        profit_loss_sek=pnl,
    )


def _options_trade(sale=200.0, cost=180.0, pnl=20.0) -> K4Trade:
    return _make_trade(
        asset_class="EQUITY AND INDEX OPTIONS",
        k4_section="D",
        sale_amount_sek=sale,
        purchase_amount_sek=cost,
        profit_loss_sek=pnl,
    )


def _fop_trade(sale=300.0, cost=280.0, pnl=20.0) -> K4Trade:
    """Options on futures — classified as FUTURES for reconciliation."""
    return _make_trade(
        asset_class="OPTIONS ON FUTURES",
        k4_section="D",
        sale_amount_sek=sale,
        purchase_amount_sek=cost,
        profit_loss_sek=pnl,
    )


# ---------------------------------------------------------------------------
# SectionTotals
# ---------------------------------------------------------------------------

class TestSectionTotals:
    def test_net_profit(self):
        s = SectionTotals(section="D", profit=344044.0, loss=199706.0)
        assert s.net == pytest.approx(144338.0)

    def test_net_zero_when_equal(self):
        s = SectionTotals(section="A", profit=100.0, loss=100.0)
        assert s.net == 0.0

    def test_net_negative(self):
        s = SectionTotals(section="D", profit=100.0, loss=200.0)
        assert s.net == pytest.approx(-100.0)


# ---------------------------------------------------------------------------
# ControlDiff
# ---------------------------------------------------------------------------

class TestControlDiff:
    def test_difference(self):
        d = ControlDiff(label="X", calculated=1050.0, skv=1000.0)
        assert d.difference == pytest.approx(50.0)

    def test_negative_difference(self):
        d = ControlDiff(label="X", calculated=950.0, skv=1000.0)
        assert d.difference == pytest.approx(-50.0)

    def test_deviation_pct(self):
        d = ControlDiff(label="X", calculated=1050.0, skv=1000.0)
        assert d.deviation_pct == pytest.approx(0.05)

    def test_ok_within_tolerance(self):
        d = ControlDiff(label="X", calculated=1040.0, skv=1000.0, tolerance=0.05)
        assert d.ok is True

    def test_not_ok_outside_tolerance(self):
        d = ControlDiff(label="X", calculated=1060.0, skv=1000.0, tolerance=0.05)
        assert d.ok is False

    def test_exact_boundary_ok(self):
        # 5.0% deviation with 5% tolerance → ok
        d = ControlDiff(label="X", calculated=1050.0, skv=1000.0, tolerance=0.05)
        assert d.ok is True

    def test_zero_skv_zero_calculated(self):
        d = ControlDiff(label="X", calculated=0.0, skv=0.0)
        assert d.deviation_pct == 0.0
        assert d.ok is True

    def test_zero_skv_nonzero_calculated(self):
        d = ControlDiff(label="X", calculated=100.0, skv=0.0)
        assert d.deviation_pct == 1.0
        assert d.ok is False


# ---------------------------------------------------------------------------
# compute_reconciliation — section totals
# ---------------------------------------------------------------------------

class TestComputeReconciliationSections:
    def test_empty_trades(self):
        result = compute_reconciliation([])
        assert result.trade_count == 0
        assert result.section_a.profit == 0.0
        assert result.section_d.profit == 0.0

    def test_single_stock_trade(self):
        trades = [_stock_trade(sale=1000.0, cost=900.0, pnl=100.0)]
        result = compute_reconciliation(trades)
        assert result.trade_count == 1
        assert result.section_a.proceeds == pytest.approx(1000.0)
        assert result.section_a.cost == pytest.approx(900.0)
        assert result.section_a.profit == pytest.approx(100.0)
        assert result.section_a.loss == 0.0
        assert result.section_a.trade_count == 1

    def test_loss_trade_goes_to_loss_not_profit(self):
        trades = [_stock_trade(sale=800.0, cost=900.0, pnl=-100.0)]
        result = compute_reconciliation(trades)
        assert result.section_a.profit == 0.0
        assert result.section_a.loss == pytest.approx(100.0)

    def test_mixed_section_a_and_d(self):
        trades = [
            _stock_trade(sale=1000.0, cost=900.0, pnl=100.0),
            _futures_trade(sale=500.0, cost=450.0, pnl=50.0),
        ]
        result = compute_reconciliation(trades)
        assert result.section_a.trade_count == 1
        assert result.section_d.trade_count == 1

    def test_section_d_net(self):
        trades = [
            _futures_trade(sale=500.0, cost=450.0, pnl=50.0),
            _options_trade(sale=200.0, cost=220.0, pnl=-20.0),
        ]
        result = compute_reconciliation(trades)
        assert result.section_d.profit == pytest.approx(50.0)
        assert result.section_d.loss == pytest.approx(20.0)
        assert result.section_d.net == pytest.approx(30.0)

    def test_multiple_trades_same_section(self):
        trades = [
            _futures_trade(sale=500.0, cost=450.0, pnl=50.0),
            _futures_trade(sale=300.0, cost=280.0, pnl=20.0),
        ]
        result = compute_reconciliation(trades)
        assert result.section_d.proceeds == pytest.approx(800.0)
        assert result.section_d.profit == pytest.approx(70.0)


# ---------------------------------------------------------------------------
# compute_reconciliation — derivative breakdown
# ---------------------------------------------------------------------------

class TestComputeReconciliationDerivatives:
    def test_futures_classified_correctly(self):
        trades = [_futures_trade(sale=1000.0, cost=900.0, pnl=100.0)]
        result = compute_reconciliation(trades)
        assert result.futures.proceeds == pytest.approx(1000.0)
        assert result.futures.trade_count == 1
        assert result.options.trade_count == 0

    def test_options_classified_correctly(self):
        trades = [_options_trade(sale=200.0, cost=180.0, pnl=20.0)]
        result = compute_reconciliation(trades)
        assert result.options.proceeds == pytest.approx(200.0)
        assert result.options.trade_count == 1
        assert result.futures.trade_count == 0

    def test_options_on_futures_counted_as_futures(self):
        trades = [_fop_trade(sale=300.0, cost=280.0, pnl=20.0)]
        result = compute_reconciliation(trades)
        assert result.futures.trade_count == 1
        assert result.options.trade_count == 0

    def test_stocks_not_in_derivatives(self):
        trades = [_stock_trade()]
        result = compute_reconciliation(trades)
        assert result.futures.trade_count == 0
        assert result.options.trade_count == 0

    def test_futures_short_code(self):
        """FUT short code (flex query format) should also be recognised."""
        trade = _make_trade(
            asset_class="FUT",
            k4_section="D",
            sale_amount_sek=600.0,
            purchase_amount_sek=550.0,
            profit_loss_sek=50.0,
        )
        result = compute_reconciliation([trade])
        assert result.futures.trade_count == 1

    def test_options_short_code(self):
        trade = _make_trade(
            asset_class="OPT",
            k4_section="D",
            sale_amount_sek=100.0,
            purchase_amount_sek=80.0,
            profit_loss_sek=20.0,
        )
        result = compute_reconciliation([trade])
        assert result.options.trade_count == 1


# ---------------------------------------------------------------------------
# compute_reconciliation — SKV comparison
# ---------------------------------------------------------------------------

class TestSkvComparison:
    def _base_trades(self):
        return [
            _futures_trade(sale=148100.0, cost=98500.0, pnl=49600.0),
            _options_trade(sale=683000.0, cost=500000.0, pnl=183000.0),
        ]

    def test_no_skv_data_no_diffs(self):
        result = compute_reconciliation(self._base_trades())
        assert result.diffs == []
        assert result.all_ok is True

    def test_futures_proceeds_ok(self):
        result = compute_reconciliation(
            self._base_trades(),
            skv_futures_proceeds=147649.0,  # ~0.3% deviation
        )
        assert len(result.diffs) == 1
        assert result.diffs[0].ok is True

    def test_futures_proceeds_warning(self):
        result = compute_reconciliation(
            self._base_trades(),
            skv_futures_proceeds=100000.0,  # large deviation
        )
        assert result.diffs[0].ok is False
        assert result.all_ok is False

    def test_futures_cost_included(self):
        result = compute_reconciliation(
            self._base_trades(),
            skv_futures_cost=98558.0,
        )
        diff = next(d for d in result.diffs if d.label == "Futures cost")
        assert diff is not None

    def test_options_proceeds_ok(self):
        result = compute_reconciliation(
            self._base_trades(),
            skv_options_proceeds=683529.0,  # ~0.08% deviation
        )
        diff = next(d for d in result.diffs if d.label == "Options proceeds")
        assert diff.ok is True

    def test_all_three_skv_args(self):
        result = compute_reconciliation(
            self._base_trades(),
            skv_futures_proceeds=147649.0,
            skv_futures_cost=98558.0,
            skv_options_proceeds=683529.0,
        )
        assert len(result.diffs) == 3

    def test_status_label_ok(self):
        result = compute_reconciliation(
            self._base_trades(),
            skv_futures_proceeds=148100.0,  # exact match
        )
        assert "OK" in result.status_label

    def test_status_label_warning(self):
        result = compute_reconciliation(
            self._base_trades(),
            skv_futures_proceeds=1.0,  # wildly wrong
        )
        assert "WARNING" in result.status_label

    def test_status_label_no_data(self):
        result = compute_reconciliation(self._base_trades())
        assert "No Skatteverket" in result.status_label

    def test_custom_tolerance(self):
        # 10% deviation; passes with 15% tolerance, fails with 5%
        result_pass = compute_reconciliation(
            self._base_trades(),
            skv_futures_proceeds=148100.0 * 0.9,
            tolerance=0.15,
        )
        result_fail = compute_reconciliation(
            self._base_trades(),
            skv_futures_proceeds=148100.0 * 0.9,
            tolerance=0.05,
        )
        assert result_pass.all_ok is True
        assert result_fail.all_ok is False


# ---------------------------------------------------------------------------
# format_report
# ---------------------------------------------------------------------------

class TestFormatReport:
    def _result(self, **skv_kwargs) -> ReconciliationResult:
        trades = [
            _stock_trade(sale=317786.76, cost=297486.66, pnl=20300.10),
            _futures_trade(sale=148100.0, cost=98500.0, pnl=49600.0),
            _options_trade(sale=683000.0, cost=500000.0, pnl=183000.0),
            _options_trade(sale=200.0, cost=700.0, pnl=-500.0),
        ]
        return compute_reconciliation(trades, **skv_kwargs)

    def test_contains_header(self):
        report = format_report(self._result())
        assert "IBKR K4 RECONCILIATION REPORT" in report

    def test_contains_trade_count(self):
        report = format_report(self._result())
        assert "Trades parsed: 4" in report

    def test_contains_section_a(self):
        report = format_report(self._result())
        assert "SECTION A" in report

    def test_contains_section_d(self):
        report = format_report(self._result())
        assert "SECTION D" in report

    def test_contains_derivatives_breakdown(self):
        report = format_report(self._result())
        assert "DERIVATIVES BREAKDOWN" in report
        assert "Futures" in report
        assert "Options" in report

    def test_no_skv_block_when_no_data(self):
        report = format_report(self._result())
        assert "SKATTEVERKET CONTROL DATA" not in report

    def test_skv_block_present_when_data_given(self):
        report = format_report(self._result(skv_futures_proceeds=147649.0))
        assert "SKATTEVERKET CONTROL DATA" in report
        assert "DIFFERENCE" in report

    def test_ok_status_in_report(self):
        report = format_report(self._result(skv_futures_proceeds=148100.0))
        assert "OK" in report

    def test_warning_marker_shown_for_large_deviation(self):
        report = format_report(self._result(skv_futures_proceeds=1.0))
        assert "EXCEEDS TOLERANCE" in report

    def test_difference_sign_positive(self):
        """Calculated > SKV → positive difference."""
        report = format_report(self._result(skv_futures_proceeds=100_000.0))
        # futures proceeds = 148100, skv = 100000, diff = +48100
        assert "+48,100" in report

    def test_difference_sign_negative(self):
        """Calculated < SKV → negative difference."""
        report = format_report(self._result(skv_futures_proceeds=200_000.0))
        # futures proceeds = 148100, skv = 200000, diff = -51900
        assert "-51,900" in report


# ---------------------------------------------------------------------------
# write_report
# ---------------------------------------------------------------------------

class TestWriteReport:
    def test_file_created(self, tmp_path):
        trades = [_stock_trade()]
        result = compute_reconciliation(trades)
        path = write_report(result, tmp_path)
        assert path.exists()

    def test_file_name(self, tmp_path):
        trades = [_stock_trade()]
        result = compute_reconciliation(trades)
        path = write_report(result, tmp_path)
        assert path.name == "reconciliation_report.txt"

    def test_file_contains_report_content(self, tmp_path):
        trades = [_stock_trade()]
        result = compute_reconciliation(trades)
        path = write_report(result, tmp_path)
        content = path.read_text(encoding="utf-8")
        assert "IBKR K4 RECONCILIATION REPORT" in content

    def test_creates_missing_output_dir(self, tmp_path):
        nested = tmp_path / "a" / "b" / "c"
        trades = [_stock_trade()]
        result = compute_reconciliation(trades)
        path = write_report(result, nested)
        assert path.exists()
