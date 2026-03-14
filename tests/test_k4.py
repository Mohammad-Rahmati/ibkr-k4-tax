"""
tests/test_k4.py
----------------
Unit tests for k4_generator.py — SEK conversion, categorisation, aggregation,
and file export.
"""

from datetime import datetime
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

import sys
sys.path.insert(0, str(Path(__file__).parent.parent))

from models import K4Trade, K4SymbolSummary, Trade
from k4_generator import (
    classify_k4_section,
    convert_trade_to_sek,
    convert_trades_to_sek,
    build_symbol_summaries,
    build_section_summaries,
    export_trades_sek,
    export_k4_summary_csv,
    export_k4_summary_json,
    generate_k4_report,
)
from fx import FXRateProvider


# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------

def _make_trade(
    symbol: str = "AAPL",
    asset_class: str = "STK",
    date_str: str = "2025-03-15 10:00:00",
    quantity: float = 10.0,
    price: float = 175.0,
    proceeds: float = 1750.0,
    fees: float = -1.0,
    currency: str = "USD",
    realized_pnl: float = 50.0,
) -> Trade:
    return Trade(
        date=datetime.fromisoformat(date_str),
        symbol=symbol,
        asset_class=asset_class,
        quantity=quantity,
        price=price,
        proceeds=proceeds,
        fees=fees,
        currency=currency,
        realized_pnl=realized_pnl,
    )


def _mock_fx_provider(rate: float = 10.5) -> FXRateProvider:
    provider = MagicMock(spec=FXRateProvider)
    provider.get_rate.return_value = rate
    provider.prefetch_rates.return_value = {}
    return provider


# ---------------------------------------------------------------------------
# classify_k4_section
# ---------------------------------------------------------------------------

class TestClassifyK4Section:
    @pytest.mark.parametrize("asset_class,expected", [
        ("STK", "A"),
        ("ETF", "A"),
        ("OPT", "D"),
        ("FOP", "D"),
        ("FUT", "D"),
        ("CFD", "D"),
        ("stk", "A"),   # lowercase
        ("opt", "D"),   # lowercase
        ("UNKNOWN", "D"),  # fallback
        ("", "D"),
    ])
    def test_mapping(self, asset_class, expected):
        assert classify_k4_section(asset_class) == expected


# ---------------------------------------------------------------------------
# convert_trade_to_sek
# ---------------------------------------------------------------------------

class TestConvertTradeToSek:
    def test_basic_conversion(self):
        trade = _make_trade(proceeds=1750.0, realized_pnl=50.0)
        provider = _mock_fx_provider(rate=10.0)
        k4 = convert_trade_to_sek(trade, provider)

        # proceeds_sek = 1750 * 10 = 17500
        assert k4.sale_amount_sek == 17500.0
        # cost_basis = 1750 - 50 = 1700; cost_sek = 1700 * 10 = 17000
        assert k4.purchase_amount_sek == 17000.0
        # profit = 17500 - 17000 = 500
        assert k4.profit_loss_sek == 500.0

    def test_section_assigned(self):
        k4 = convert_trade_to_sek(_make_trade(asset_class="STK"), _mock_fx_provider())
        assert k4.k4_section == "A"

        k4 = convert_trade_to_sek(_make_trade(asset_class="OPT"), _mock_fx_provider())
        assert k4.k4_section == "D"

    def test_loss_trade(self):
        trade = _make_trade(proceeds=-500.0, realized_pnl=-100.0)
        provider = _mock_fx_provider(rate=10.0)
        k4 = convert_trade_to_sek(trade, provider)
        # proceeds_sek = -500 * 10 = -5000
        # cost_basis = -500 - (-100) = -400; cost_sek = -4000
        # pnl = -5000 - (-4000) = -1000
        assert k4.profit_loss_sek == -1000.0

    def test_sek_trade_rate_is_one(self):
        trade = _make_trade(currency="SEK", proceeds=1000.0, realized_pnl=100.0)
        provider = _mock_fx_provider(rate=1.0)
        provider.get_rate.return_value = 1.0
        k4 = convert_trade_to_sek(trade, provider)
        assert k4.sale_amount_sek == 1000.0

    def test_symbol_preserved(self):
        trade = _make_trade(symbol="TSLA")
        k4 = convert_trade_to_sek(trade, _mock_fx_provider())
        assert k4.symbol == "TSLA"

    def test_date_preserved(self):
        trade = _make_trade(date_str="2025-07-04 09:30:00")
        k4 = convert_trade_to_sek(trade, _mock_fx_provider())
        assert k4.date == datetime(2025, 7, 4, 9, 30, 0)


# ---------------------------------------------------------------------------
# convert_trades_to_sek
# ---------------------------------------------------------------------------

class TestConvertTradesToSek:
    def test_empty_input(self):
        result = convert_trades_to_sek([], _mock_fx_provider())
        assert result == []

    def test_all_trades_converted(self):
        trades = [
            _make_trade(symbol="AAPL"),
            _make_trade(symbol="MSFT"),
            _make_trade(symbol="QQQ", asset_class="OPT"),
        ]
        result = convert_trades_to_sek(trades, _mock_fx_provider(rate=10.0))
        assert len(result) == 3
        symbols = {k.symbol for k in result}
        assert symbols == {"AAPL", "MSFT", "QQQ"}


# ---------------------------------------------------------------------------
# build_symbol_summaries
# ---------------------------------------------------------------------------

class TestBuildSymbolSummaries:
    def _make_k4_trade(self, symbol, section, sale, cost, pnl) -> K4Trade:
        return K4Trade(
            date=datetime(2025, 1, 1),
            symbol=symbol,
            asset_class="STK" if section == "A" else "OPT",
            quantity=1.0,
            sale_amount_sek=sale,
            purchase_amount_sek=cost,
            profit_loss_sek=pnl,
            k4_section=section,
        )

    def test_aggregation_single_symbol(self):
        trades = [
            self._make_k4_trade("AAPL", "A", 1000.0, 900.0, 100.0),
            self._make_k4_trade("AAPL", "A", 2000.0, 1800.0, 200.0),
        ]
        summaries = build_symbol_summaries(trades)
        assert len(summaries) == 1
        s = summaries[0]
        assert s.symbol == "AAPL"
        assert s.total_sales_sek == 3000.0
        assert s.total_cost_sek == 2700.0
        assert s.profit_sek == 300.0
        assert s.loss_sek == 0.0

    def test_loss_separated_from_profit(self):
        trades = [
            self._make_k4_trade("AAPL", "A", 1000.0, 900.0, 100.0),
            self._make_k4_trade("AAPL", "A", 500.0, 700.0, -200.0),
        ]
        summaries = build_symbol_summaries(trades)
        s = summaries[0]
        assert s.profit_sek == 100.0
        assert s.loss_sek == 200.0

    def test_multiple_symbols(self):
        trades = [
            self._make_k4_trade("AAPL", "A", 1000.0, 800.0, 200.0),
            self._make_k4_trade("MSFT", "A", 2000.0, 1500.0, 500.0),
            self._make_k4_trade("SPY", "D", 300.0, 350.0, -50.0),
        ]
        summaries = build_symbol_summaries(trades)
        assert len(summaries) == 3

    def test_sorted_by_section_then_symbol(self):
        trades = [
            self._make_k4_trade("MSFT", "A", 100, 80, 20),
            self._make_k4_trade("AAPL", "A", 100, 80, 20),
            self._make_k4_trade("ZZZ", "D", 100, 80, 20),
        ]
        summaries = build_symbol_summaries(trades)
        assert summaries[0].symbol == "AAPL"
        assert summaries[1].symbol == "MSFT"
        assert summaries[2].symbol == "ZZZ"


# ---------------------------------------------------------------------------
# build_section_summaries
# ---------------------------------------------------------------------------

class TestBuildSectionSummaries:
    def test_section_a_and_d_present(self):
        result = build_section_summaries([])
        assert "section_a" in result
        assert "section_d" in result

    def test_aggregation(self):
        sym_sums = [
            K4SymbolSummary(
                symbol="AAPL", k4_section="A",
                total_sales_sek=1000, total_cost_sek=800,
                profit_sek=200, loss_sek=0,
            ),
            K4SymbolSummary(
                symbol="QQQ", k4_section="D",
                total_sales_sek=500, total_cost_sek=600,
                profit_sek=0, loss_sek=100,
            ),
        ]
        result = build_section_summaries(sym_sums)
        assert result["section_a"].total_sales == 1000
        assert result["section_a"].profit == 200
        assert result["section_d"].loss == 100


# ---------------------------------------------------------------------------
# Export functions
# ---------------------------------------------------------------------------

class TestExportTradesSek:
    def test_creates_file(self, tmp_path):
        k4_trades = [
            K4Trade(
                date=datetime(2025, 3, 15),
                symbol="AAPL",
                asset_class="STK",
                quantity=10.0,
                sale_amount_sek=17500.0,
                purchase_amount_sek=17000.0,
                profit_loss_sek=500.0,
                k4_section="A",
            )
        ]
        path = export_trades_sek(k4_trades, tmp_path)
        assert path.exists()
        content = path.read_text()
        assert "AAPL" in content
        assert "17500" in content

    def test_empty_trades_creates_empty_csv(self, tmp_path):
        path = export_trades_sek([], tmp_path)
        assert path.exists()


class TestExportK4SummaryJson:
    def test_json_structure(self, tmp_path):
        import json
        from models import K4SectionSummary

        summaries = {
            "section_a": K4SectionSummary(total_sales=1000, total_cost=800, profit=200, loss=0),
            "section_d": K4SectionSummary(total_sales=500, total_cost=600, profit=0, loss=100),
        }
        path = export_k4_summary_json(summaries, tmp_path)
        data = json.loads(path.read_text())
        assert "section_a" in data
        assert "section_d" in data
        assert data["section_a"]["total_sales"] == 1000
        assert data["section_d"]["loss"] == 100


# ---------------------------------------------------------------------------
# generate_k4_report (integration smoke test)
# ---------------------------------------------------------------------------

class TestGenerateK4Report:
    def test_generates_all_three_files(self, tmp_path):
        trades = [
            _make_trade(symbol="AAPL", asset_class="STK", proceeds=1750.0, realized_pnl=50.0),
            _make_trade(symbol="QQQ", asset_class="OPT", proceeds=250.0, realized_pnl=100.0),
        ]
        provider = _mock_fx_provider(rate=10.0)
        paths = generate_k4_report(trades, tmp_path, fx_provider=provider)
        assert len(paths) == 3
        for p in paths.values():
            assert p.exists()

    def test_empty_trades_generates_files(self, tmp_path):
        provider = _mock_fx_provider(rate=10.0)
        paths = generate_k4_report([], tmp_path, fx_provider=provider)
        assert len(paths) == 3
