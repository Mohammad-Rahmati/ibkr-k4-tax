"""
tests/test_parser.py
--------------------
Unit tests for parser.py — IBKR Activity Statement CSV parsing.

Uses the real IBKR Activity Statement column format:
  DataDiscriminator, Asset Category, Currency, Symbol, Date/Time,
  Quantity, T. Price, C. Price, Proceeds, Comm/Fee, Basis, Realized P/L, MTM P/L, Code
"""

import io
import textwrap
from datetime import datetime
from pathlib import Path

import pytest

import sys
sys.path.insert(0, str(Path(__file__).parent.parent))

from parser import (
    _iter_sections,
    parse_activity_statement,
    extract_trades,
    load_trades,
    _parse_trade_row,
)
from models import Trade


# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------

# Real IBKR Activity Statement format:
# - Column 0: Section name, Column 1: Row type
# - Trades section: DataDiscriminator distinguishes Order from SubTotal/Total
# - Date/Time format: "YYYY-MM-DD, HH:MM:SS" (quoted with comma)
MINIMAL_CSV = textwrap.dedent("""\
    Statement,Header,Field Name,Field Value
    Statement,Data,BrokerName,Interactive Brokers Ireland Limited
    Statement,Data,Title,Activity Statement
    Trades,Header,DataDiscriminator,Asset Category,Currency,Symbol,Date/Time,Quantity,T. Price,C. Price,Proceeds,Comm/Fee,Basis,Realized P/L,MTM P/L,Code
    Trades,Data,Order,Stocks,USD,AAPL,"2025-03-15, 10:00:00",10,175.00,175.00,1750.00,-1.00,-1700.00,50.00,0.00,C
    Trades,Data,SubTotal,,Stocks,USD,AAPL,,,,,1750.00,-1.00,-1700.00,50.00,0.00,
    Trades,Data,Order,Stocks,USD,QQQ,"2025-06-20, 14:30:00",-5,410.00,410.00,-2050.00,-0.75,2100.00,-25.00,0.00,C
    Trades,Data,Order,Equity and Index Options,USD,SPY OPT,"2025-09-10, 09:15:00",1,2.50,2.50,250.00,-1.25,-150.00,100.00,0.00,C
    Net Asset Value,Header,Asset Class,Prior Total,Current Total,Change
    Net Asset Value,Data,Cash,10000,12000,2000
""")


def _make_fileobj(content: str) -> io.StringIO:
    return io.StringIO(content)


def _write_tmp_csv(tmp_path: Path, content: str) -> Path:
    p = tmp_path / "activity.csv"
    p.write_text(content, encoding="utf-8")
    return p


# ---------------------------------------------------------------------------
# _iter_sections
# ---------------------------------------------------------------------------

class TestIterSections:
    def test_detects_all_sections(self):
        fh = _make_fileobj(MINIMAL_CSV)
        sections = {name: rows for name, _headers, rows in _iter_sections(fh)}
        assert "Statement" in sections
        assert "Trades" in sections
        assert "Net Asset Value" in sections

    def test_trades_section_row_count(self):
        """SubTotal row is a Data row too, so total is 4 data rows."""
        fh = _make_fileobj(MINIMAL_CSV)
        sections = {name: rows for name, _headers, rows in _iter_sections(fh)}
        assert len(sections["Trades"]) == 4  # 3 Orders + 1 SubTotal

    def test_header_columns_lowercased(self):
        fh = _make_fileobj(MINIMAL_CSV)
        for name, headers, rows in _iter_sections(fh):
            if name == "Trades":
                assert "datadiscriminator" in headers
                assert "symbol" in headers
                assert "currency" in headers

    def test_data_row_keys_match_headers(self):
        fh = _make_fileobj(MINIMAL_CSV)
        for name, headers, rows in _iter_sections(fh):
            if name == "Trades":
                for row in rows:
                    for h in headers:
                        assert h in row

    def test_empty_csv_yields_nothing(self):
        fh = _make_fileobj("")
        result = list(_iter_sections(fh))
        assert result == []

    def test_missing_data_before_header_skipped(self):
        """Data rows that appear before their section's Header should be skipped."""
        csv_content = textwrap.dedent("""\
            Orphan,Data,some,value
            Orphan,Header,col1,col2
        """)
        fh = _make_fileobj(csv_content)
        sections = {name: rows for name, _headers, rows in _iter_sections(fh)}
        # The data row appeared before the header — it should be dropped.
        assert sections.get("Orphan", []) == []


# ---------------------------------------------------------------------------
# _parse_trade_row
# ---------------------------------------------------------------------------

class TestParseTradeRow:
    def _sample_row(self, **overrides) -> dict:
        base = {
            "datadiscriminator": "Order",
            "date/time": "2025-03-15, 10:00:00",
            "symbol": "AAPL",
            "asset category": "Stocks",
            "quantity": "10",
            "t. price": "175.00",
            "proceeds": "1750.00",
            "comm/fee": "-1.00",
            "currency": "USD",
            "realized p/l": "50.00",
        }
        base.update(overrides)
        return base

    def test_valid_row_returns_trade(self):
        row = self._sample_row()
        trade = _parse_trade_row(row, 0)
        assert isinstance(trade, Trade)
        assert trade.symbol == "AAPL"
        assert trade.asset_class == "STOCKS"   # normalised to uppercase
        assert trade.quantity == 10.0
        assert trade.proceeds == 1750.0
        assert trade.fees == -1.0
        assert trade.realized_pnl == 50.0
        assert trade.currency == "USD"

    def test_date_parsed_correctly(self):
        trade = _parse_trade_row(self._sample_row(), 0)
        assert trade.date == datetime(2025, 3, 15, 10, 0, 0)

    def test_subtotal_row_returns_none(self):
        row = self._sample_row(datadiscriminator="SubTotal")
        assert _parse_trade_row(row, 0) is None

    def test_empty_quantity_returns_none(self):
        row = self._sample_row(quantity="")
        assert _parse_trade_row(row, 0) is None

    def test_non_numeric_quantity_returns_none(self):
        # "Quantity" header string appearing as data would fail numeric coercion
        row = self._sample_row(quantity="Quantity")
        assert _parse_trade_row(row, 0) is None

    def test_invalid_date_returns_none(self):
        row = self._sample_row(**{"date/time": "not-a-date"})
        assert _parse_trade_row(row, 0) is None

    def test_comma_separated_numbers_parsed(self):
        row = self._sample_row(proceeds="1,750.00", **{"realized p/l": "50.00"})
        trade = _parse_trade_row(row, 0)
        assert trade is not None
        assert trade.proceeds == 1750.0

    def test_negative_quantity(self):
        row = self._sample_row(quantity="-5")
        trade = _parse_trade_row(row, 0)
        assert trade is not None
        assert trade.quantity == -5.0


# ---------------------------------------------------------------------------
# extract_trades
# ---------------------------------------------------------------------------

class TestExtractTrades:
    def _sections_from_csv(self, content: str) -> dict:
        fh = _make_fileobj(content)
        return {name: rows for name, _headers, rows in _iter_sections(fh)}

    def test_extracts_all_trades_no_filter(self):
        """Only Order rows (not SubTotal) are extracted."""
        sections = self._sections_from_csv(MINIMAL_CSV)
        trades = extract_trades(sections)
        assert len(trades) == 3

    def test_start_date_filter(self):
        sections = self._sections_from_csv(MINIMAL_CSV)
        trades = extract_trades(sections, start=datetime(2025, 6, 1))
        dates = [t.date for t in trades]
        assert all(d >= datetime(2025, 6, 1) for d in dates)
        assert len(trades) == 2

    def test_end_date_filter(self):
        sections = self._sections_from_csv(MINIMAL_CSV)
        trades = extract_trades(sections, end=datetime(2025, 6, 30))
        assert len(trades) == 2

    def test_date_range_filter(self):
        sections = self._sections_from_csv(MINIMAL_CSV)
        trades = extract_trades(
            sections,
            start=datetime(2025, 6, 1),
            end=datetime(2025, 6, 30),
        )
        assert len(trades) == 1
        assert trades[0].symbol == "QQQ"

    def test_sorted_ascending_by_date(self):
        sections = self._sections_from_csv(MINIMAL_CSV)
        trades = extract_trades(sections)
        dates = [t.date for t in trades]
        assert dates == sorted(dates)

    def test_no_trades_section_returns_empty(self):
        csv = textwrap.dedent("""\
            Statement,Header,Field Name,Field Value
            Statement,Data,BrokerName,IBKR
        """)
        sections = self._sections_from_csv(csv)
        assert extract_trades(sections) == []


# ---------------------------------------------------------------------------
# load_trades (integration)
# ---------------------------------------------------------------------------

class TestLoadTrades:
    def test_load_trades_from_file(self, tmp_path):
        csv_path = _write_tmp_csv(tmp_path, MINIMAL_CSV)
        trades = load_trades(csv_path)
        assert len(trades) == 3

    def test_load_trades_with_date_filter(self, tmp_path):
        csv_path = _write_tmp_csv(tmp_path, MINIMAL_CSV)
        trades = load_trades(
            csv_path,
            start=datetime(2025, 1, 1),
            end=datetime(2025, 5, 31),
        )
        assert len(trades) == 1
        assert trades[0].symbol == "AAPL"

    def test_missing_file_raises(self, tmp_path):
        with pytest.raises(FileNotFoundError):
            load_trades(tmp_path / "nonexistent.csv")

    def test_asset_classes_preserved(self, tmp_path):
        csv_path = _write_tmp_csv(tmp_path, MINIMAL_CSV)
        trades = load_trades(csv_path)
        asset_classes = {t.asset_class for t in trades}
        # Asset classes are normalised to uppercase
        assert "STOCKS" in asset_classes
        assert "EQUITY AND INDEX OPTIONS" in asset_classes
