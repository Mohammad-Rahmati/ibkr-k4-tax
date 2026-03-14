"""
tests/test_skv_parser.py
-------------------------
Unit tests for skv_parser.py.

All tests use synthetic text strings fed directly into ``parse_skv_text()``,
so no actual PDF file is required.
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from skv_parser import (
    SkvControlData,
    SkvParseError,
    _normalise_line,
    _parse_number,
    _extract_last_number,
    parse_skv_text,
    parse_skv_pdf,
)


# ---------------------------------------------------------------------------
# Helpers — synthetic PDF texts
# ---------------------------------------------------------------------------

_FULL_TEXT = """\
Kontroll- och inkomstuppgifter 2025
Interactive Brokers LLC

Övriga värdepapper

Övriga terminer - erhållen ersättning
147 649

Övriga terminer - erlagd ersättning
98 558

Övriga optioner
683 529

Summa
929 736
"""

_ENDASH_TEXT = """\
Övriga terminer – erhållen ersättning
147649
Övriga terminer – erlagd ersättning
98558
Övriga optioner
683529
"""

_SAME_LINE_TEXT = """\
Övriga terminer - erhållen ersättning  147 649
Övriga terminer - erlagd ersättning    98 558
Övriga optioner                        683 529
"""

_COMMA_SEP_TEXT = """\
Övriga terminer - erhållen ersättning
147,649
Övriga terminer - erlagd ersättning
98,558
Övriga optioner
683,529
"""

_PARTIAL_TEXT = """\
Övriga terminer - erhållen ersättning
147 649
"""

# ---------------------------------------------------------------------------
# Real-PDF format: Interactive Brokers data rows + Summa line
# Mirrors the exact structure of Skatteverket's Kontroll- och inkomstuppgifter
# ---------------------------------------------------------------------------

# Compact 3-section real-PDF text with multi-row IB data and Summa lines.
_REAL_PDF_TEXT = """\
Övriga terminer - erhållen ersättning
Interactive Brokers Ireland Limited MES 19DEC25 (141 st) 27 892
Interactive Brokers Ireland Limited MES 19SEP25 (187 st) 24 226
Interactive Brokers Ireland Limited MES 20JUN25 (28 st) 82 389
Interactive Brokers Ireland Limited MNQ 19DEC25 26
Interactive Brokers Ireland Limited MNQ 19SEP25 89
Interactive Brokers Ireland Limited MNQ 21MAR25 (8 st) 12 074
Interactive Brokers Ireland Limited VXM 17SEP25 (6 st) 32
Interactive Brokers Ireland Limited VXM 19NOV25 (13 st) 892
Interactive Brokers Ireland Limited VXM 20AUG25 29
Summa 147 649
Övriga terminer - erlagd ersättning
Interactive Brokers Ireland Limited MES 20MAR26 (15 st) 4 130
Interactive Brokers Ireland Limited MES 21MAR25 (10 st) 85 919
Interactive Brokers Ireland Limited VIX 17SEP25 (2 st) 568
Interactive Brokers Ireland Limited VIX 19FEB25 (4 st) 1 595
Interactive Brokers Ireland Limited VXM 17DEC25 (17 st) 4 927
Interactive Brokers Ireland Limited VXM 21JAN26 (31 st) 1 093
Interactive Brokers Ireland Limited VXM 22OCT25 326
Summa 98 558
Övriga optioner
Interactive Brokers Ireland Limited IBIT 01AUG25 64.5 P 164
Interactive Brokers Ireland Limited IBIT 03JAN25 52.5 P (5 st) 4 095
Interactive Brokers Ireland Limited MES 05SEP25 6610 C 0
Interactive Brokers Ireland Limited MES 06AUG25 6350 C 399
Interactive Brokers Ireland Limited QQQ 17JAN25 510 P (2 st) 2 162
Summa 6 820
"""

# Real-PDF text with page-break noise between section header and first IB row.
# Mirrors the real PDF where "Övriga optioner" is on page 5 and the data
# rows start on page 6, with page-header junk in between.
_REAL_PDF_PAGEBREAK_TEXT = """\
Övriga terminer - erhållen ersättning
Interactive Brokers Ireland Limited MES 20JUN25 (28 st) 82 389
Interactive Brokers Ireland Limited MNQ 21MAR25 (8 st) 12 074
Summa 94 463
Övriga terminer - erlagd ersättning
Interactive Brokers Ireland Limited MES 21MAR25 (10 st) 85 919
Interactive Brokers Ireland Limited VXM 22OCT25 326
Summa 86 245
Övriga optioner
* Kontroll- och inkomstuppgifter 2 (6)
Skatteverket
20
00
vs
60
8004
VKS
Interactive Brokers Ireland Limited IBIT 03JAN25 52.5 P (5 st) 4 095
Interactive Brokers Ireland Limited IBIT 07FEB25 55.5 C (5 st) 6 817
Interactive Brokers Ireland Limited MES 05SEP25 6610 C 0
Summa 10 912
"""

_MISSING_TEXT = """\
Inkomst av kapital
Ränteinkomster 1 500
Utdelning 3 000
"""

_MULTIPAGE_TEXT = """\
Sida 1 av 3
Inkomstdeklaration 1

Övriga terminer - erhållen ersättning
147 649

--- SIDBRYTNING ---

Övriga terminer - erlagd ersättning
98 558

--- SIDBRYTNING ---

Övriga optioner
683 529
"""

_LARGE_NUMBERS_TEXT = """\
Övriga terminer - erhållen ersättning
1 234 567
Övriga terminer - erlagd ersättning
987 654
Övriga optioner
2 000 000
"""


# ---------------------------------------------------------------------------
# _normalise_line
# ---------------------------------------------------------------------------

class TestNormaliseLine:
    def test_lower_case(self):
        assert _normalise_line("Övriga Terminer") == "övriga terminer"

    def test_strips_whitespace(self):
        assert _normalise_line("  hello world  ") == "hello world"

    def test_collapses_internal_spaces(self):
        assert _normalise_line("a   b   c") == "a b c"

    def test_empty_string(self):
        assert _normalise_line("") == ""

    def test_preserves_swedish_chars(self):
        norm = _normalise_line("Övriga terminer – erhållen ersättning")
        assert "övriga" in norm
        assert "erhållen" in norm


# ---------------------------------------------------------------------------
# _parse_number
# ---------------------------------------------------------------------------

class TestParseNumber:
    def test_plain_integer(self):
        assert _parse_number("147649") == 147649.0

    def test_space_thousands_sep(self):
        assert _parse_number("147 649") == 147649.0

    def test_comma_thousands_sep(self):
        assert _parse_number("147,649") == 147649.0

    def test_negative_value(self):
        assert _parse_number("-98 558") == -98558.0

    def test_inline_with_label(self):
        # Number embedded after label text
        assert _parse_number("Övriga optioner 683 529") == 683529.0

    def test_large_number_with_two_spaces(self):
        assert _parse_number("1 234 567") == 1234567.0

    def test_no_digits_returns_none(self):
        assert _parse_number("Summa") is None

    def test_empty_string_returns_none(self):
        assert _parse_number("") is None

    def test_zero(self):
        assert _parse_number("0") == 0.0


# ---------------------------------------------------------------------------
# parse_skv_text — happy path
# ---------------------------------------------------------------------------

class TestParseSkvTextHappyPath:
    def test_all_three_fields_hyphen(self):
        data = parse_skv_text(_FULL_TEXT)
        assert data.futures_proceeds == pytest.approx(147649.0)
        assert data.futures_cost == pytest.approx(98558.0)
        assert data.options_proceeds == pytest.approx(683529.0)

    def test_all_three_fields_endash(self):
        data = parse_skv_text(_ENDASH_TEXT)
        assert data.futures_proceeds == pytest.approx(147649.0)
        assert data.futures_cost == pytest.approx(98558.0)
        assert data.options_proceeds == pytest.approx(683529.0)

    def test_value_on_same_line(self):
        data = parse_skv_text(_SAME_LINE_TEXT)
        assert data.futures_proceeds == pytest.approx(147649.0)
        assert data.futures_cost == pytest.approx(98558.0)
        assert data.options_proceeds == pytest.approx(683529.0)

    def test_comma_formatted_numbers(self):
        data = parse_skv_text(_COMMA_SEP_TEXT)
        assert data.futures_proceeds == pytest.approx(147649.0)
        assert data.futures_cost == pytest.approx(98558.0)
        assert data.options_proceeds == pytest.approx(683529.0)

    def test_multipage_text(self):
        data = parse_skv_text(_MULTIPAGE_TEXT)
        assert data.futures_proceeds == pytest.approx(147649.0)
        assert data.futures_cost == pytest.approx(98558.0)
        assert data.options_proceeds == pytest.approx(683529.0)

    def test_large_numbers(self):
        data = parse_skv_text(_LARGE_NUMBERS_TEXT)
        assert data.futures_proceeds == pytest.approx(1234567.0)
        assert data.futures_cost == pytest.approx(987654.0)
        assert data.options_proceeds == pytest.approx(2000000.0)

    def test_no_space_numbers(self):
        text = (
            "Övriga terminer - erhållen ersättning\n147649\n"
            "Övriga terminer - erlagd ersättning\n98558\n"
            "Övriga optioner\n683529\n"
        )
        data = parse_skv_text(text)
        assert data.futures_proceeds == pytest.approx(147649.0)
        assert data.futures_cost == pytest.approx(98558.0)
        assert data.options_proceeds == pytest.approx(683529.0)


# ---------------------------------------------------------------------------
# parse_skv_text — partial / missing
# ---------------------------------------------------------------------------

class TestParseSkvTextPartial:
    def test_only_futures_proceeds(self):
        data = parse_skv_text(_PARTIAL_TEXT)
        assert data.futures_proceeds == pytest.approx(147649.0)
        assert data.futures_cost is None
        assert data.options_proceeds is None

    def test_no_relevant_phrases(self):
        data = parse_skv_text(_MISSING_TEXT)
        assert data.futures_proceeds is None
        assert data.futures_cost is None
        assert data.options_proceeds is None
        assert data.any_found() is False

    def test_empty_text(self):
        data = parse_skv_text("")
        assert not data.any_found()

    def test_only_options_proceeds(self):
        text = "Övriga optioner\n683 529\n"
        data = parse_skv_text(text)
        assert data.options_proceeds == pytest.approx(683529.0)
        assert data.futures_proceeds is None
        assert data.futures_cost is None

    def test_duplicate_phrase_uses_first_occurrence(self):
        """If the same phrase appears twice, the first value wins."""
        text = (
            "Övriga optioner\n100 000\n"
            "Övriga optioner\n200 000\n"
        )
        data = parse_skv_text(text)
        assert data.options_proceeds == pytest.approx(100000.0)


# ---------------------------------------------------------------------------
# SkvControlData helpers
# ---------------------------------------------------------------------------

class TestSkvControlData:
    def test_any_found_true(self):
        d = SkvControlData(futures_proceeds=147649.0)
        assert d.any_found() is True

    def test_any_found_false(self):
        d = SkvControlData()
        assert d.any_found() is False

    def test_as_skv_kwargs_all(self):
        d = SkvControlData(
            futures_proceeds=147649.0,
            futures_cost=98558.0,
            options_proceeds=683529.0,
        )
        kw = d.as_skv_kwargs()
        assert kw == {
            "skv_futures_proceeds": 147649.0,
            "skv_futures_cost": 98558.0,
            "skv_options_proceeds": 683529.0,
        }

    def test_as_skv_kwargs_partial(self):
        d = SkvControlData(futures_proceeds=147649.0)
        kw = d.as_skv_kwargs()
        assert "skv_futures_proceeds" in kw
        assert "skv_futures_cost" not in kw
        assert "skv_options_proceeds" not in kw

    def test_as_skv_kwargs_empty(self):
        d = SkvControlData()
        assert d.as_skv_kwargs() == {}

    def test_str_non_empty(self):
        d = SkvControlData(futures_proceeds=147649.0, options_proceeds=683529.0)
        s = str(d)
        assert "futures_proceeds" in s
        assert "options_proceeds" in s
        assert "futures_cost" not in s

    def test_str_empty(self):
        d = SkvControlData()
        assert "empty" in str(d)

    def test_source_pdf_stored(self):
        d = parse_skv_text(_FULL_TEXT, source_pdf=Path("/tmp/test.pdf"))
        assert d.source_pdf == Path("/tmp/test.pdf")


# ---------------------------------------------------------------------------
# parse_skv_pdf — file-level
# ---------------------------------------------------------------------------

class TestParseSkvPdf:
    def test_file_not_found_raises(self, tmp_path):
        with pytest.raises(FileNotFoundError):
            parse_skv_pdf(tmp_path / "nonexistent.pdf")

    def test_pdfplumber_not_installed_raises(self, tmp_path):
        """If pdfplumber raises ImportError, SkvParseError should be raised."""
        fake_pdf = tmp_path / "fake.pdf"
        fake_pdf.write_bytes(b"%PDF-1.4 fake")

        # Patch pdfplumber.open to simulate ImportError in the helper
        with patch("skv_parser._extract_text_from_pdf", side_effect=SkvParseError("no pdfplumber")):
            with pytest.raises(SkvParseError, match="no pdfplumber"):
                parse_skv_pdf(fake_pdf)

    def test_successful_parse_via_mocked_pdfplumber(self, tmp_path):
        """Use a mock pdfplumber to verify the full parse_skv_pdf flow."""
        fake_pdf = tmp_path / "deklaration.pdf"
        fake_pdf.write_bytes(b"%PDF-1.4 fake content")

        # Mock _extract_text_from_pdf to return synthetic text
        with patch(
            "skv_parser._extract_text_from_pdf",
            return_value=_FULL_TEXT,
        ):
            data = parse_skv_pdf(fake_pdf)

        assert data.futures_proceeds == pytest.approx(147649.0)
        assert data.futures_cost == pytest.approx(98558.0)
        assert data.options_proceeds == pytest.approx(683529.0)
        assert data.source_pdf == fake_pdf


# ---------------------------------------------------------------------------
# Integration with reconciliation
# ---------------------------------------------------------------------------

class TestIntegrationWithReconciliation:
    """Verify that SkvControlData.as_skv_kwargs() feeds correctly into compute_reconciliation."""

    def test_round_trip(self):
        from models import K4Trade
        from datetime import datetime
        from reconciliation import compute_reconciliation

        # Futures SKV split uses per-symbol net profit_loss_sek.
        # Use distinct symbols so the two trades land in separate SKV buckets.
        # "ESH5" (net P&L positive) → skv_proceeds; "NQH5" (net P&L negative) → skv_cost.
        trades = [
            K4Trade(
                date=datetime(2025, 3, 15),
                symbol="ESH5",
                asset_class="FUTURES",
                quantity=5.0,
                sale_amount_sek=147649.0,
                purchase_amount_sek=0.0,
                profit_loss_sek=147649.0,   # net P&L positive → skv_proceeds
                k4_section="D",
            ),
            K4Trade(
                date=datetime(2025, 3, 20),
                symbol="NQH5",
                asset_class="FUTURES",
                quantity=5.0,
                sale_amount_sek=-98558.0,
                purchase_amount_sek=0.0,
                profit_loss_sek=-98558.0,   # net P&L negative → skv_cost (abs)
                k4_section="D",
            ),
            K4Trade(
                date=datetime(2025, 6, 10),
                symbol="SPX",
                asset_class="EQUITY AND INDEX OPTIONS",
                quantity=10.0,
                sale_amount_sek=683529.0,   # positive premium → skv_proceeds
                purchase_amount_sek=500000.0,
                profit_loss_sek=183529.0,
                k4_section="D",
            ),
        ]

        skv_data = parse_skv_text(_FULL_TEXT)
        result = compute_reconciliation(trades, **skv_data.as_skv_kwargs())

        assert len(result.diffs) == 3
        # All values match exactly → all diffs OK
        assert result.all_ok is True
        assert "OK" in result.status_label


# ---------------------------------------------------------------------------
# _extract_last_number
# ---------------------------------------------------------------------------

class TestExtractLastNumber:
    def test_plain_value_at_end(self):
        assert _extract_last_number("Interactive Brokers Ireland Limited MNQ 19DEC25 26") == 26

    def test_space_thousands_at_end(self):
        assert _extract_last_number("Interactive Brokers Ireland Limited MES 19DEC25 (141 st) 27 892") == 27892

    def test_paren_quantity_ignored(self):
        # (141 st) must not bleed into the extracted number
        assert _extract_last_number("Interactive Brokers Ireland Limited MES 20JUN25 (28 st) 82 389") == 82389

    def test_multiple_parens(self):
        assert _extract_last_number("Foo (5 st) bar (2 st) 4 095") == 4095

    def test_zero_amount(self):
        assert _extract_last_number("Interactive Brokers Ireland Limited MES 05SEP25 6610 C 0") == 0

    def test_summa_line(self):
        assert _extract_last_number("Summa 147 649") == 147649

    def test_large_number(self):
        assert _extract_last_number("Interactive Brokers Ireland Limited X 1 234 567") == 1234567

    def test_no_number_returns_none(self):
        assert _extract_last_number("Skatteverket") is None

    def test_empty_string_returns_none(self):
        assert _extract_last_number("") is None

    def test_paren_only_content_ignored(self):
        # After stripping parens nothing with digits remains
        assert _extract_last_number("foo (141 st)") is None

    def test_single_digit(self):
        assert _extract_last_number("something 5") == 5

    def test_option_symbol_not_last(self):
        # Symbol like "6610 C" should not be mistaken as the amount; 0 is last
        line = "Interactive Brokers Ireland Limited MES 05SEP25 6610 C 0"
        assert _extract_last_number(line) == 0


# ---------------------------------------------------------------------------
# parse_skv_text — real-PDF format (IB rows + Summa)
# ---------------------------------------------------------------------------

class TestRealPdfFormat:
    """Tests using synthetic text that mirrors the actual Skatteverket PDF structure."""

    def test_all_three_sections_with_summa(self):
        data = parse_skv_text(_REAL_PDF_TEXT)
        assert data.futures_proceeds == pytest.approx(147649.0)
        assert data.futures_cost == pytest.approx(98558.0)
        assert data.options_proceeds == pytest.approx(6820.0)

    def test_summa_is_authoritative_over_row_sum(self):
        """When Summa disagrees with row sum, Summa must win."""
        text = (
            "Övriga terminer - erhållen ersättning\n"
            "Interactive Brokers Ireland Limited MES 19DEC25 (141 st) 27 892\n"
            "Interactive Brokers Ireland Limited MES 19SEP25 87\n"
            "Summa 27 980\n"   # 27892 + 87 = 27979, Summa says 27980
        )
        data = parse_skv_text(text)
        assert data.futures_proceeds == pytest.approx(27980.0)

    def test_paren_quantity_not_extracted(self):
        """Values like (141 st) must never be extracted as the row amount."""
        text = (
            "Övriga terminer - erhållen ersättning\n"
            "Interactive Brokers Ireland Limited MES 19DEC25 (141 st) 27 892\n"
            "Summa 27 892\n"
        )
        data = parse_skv_text(text)
        assert data.futures_proceeds == pytest.approx(27892.0)

    def test_pagebreak_noise_ignored(self):
        """Page headers and form numbers between section header and IB rows must not
        cause the section value to be extracted prematurely."""
        data = parse_skv_text(_REAL_PDF_PAGEBREAK_TEXT)
        assert data.futures_proceeds == pytest.approx(94463.0)
        assert data.futures_cost == pytest.approx(86245.0)
        assert data.options_proceeds == pytest.approx(10912.0)

    def test_pagebreak_noise_does_not_use_small_number(self):
        """Stray page-header numbers (20, 60, 8004) must not be taken as the value."""
        data = parse_skv_text(_REAL_PDF_PAGEBREAK_TEXT)
        assert data.options_proceeds != pytest.approx(20.0)
        assert data.options_proceeds != pytest.approx(60.0)
        assert data.options_proceeds != pytest.approx(8004.0)

    def test_row_sum_used_when_no_summa(self):
        """Without a Summa line, sum the IB row values."""
        text = (
            "Övriga terminer - erhållen ersättning\n"
            "Interactive Brokers Ireland Limited MES 19DEC25 100\n"
            "Interactive Brokers Ireland Limited MES 19SEP25 200\n"
            "Övriga terminer - erlagd ersättning\n"
        )
        data = parse_skv_text(text)
        assert data.futures_proceeds == pytest.approx(300.0)

    def test_single_ib_row_no_summa(self):
        text = (
            "Övriga optioner\n"
            "Interactive Brokers Ireland Limited IBIT 01AUG25 P 5 000\n"
        )
        data = parse_skv_text(text)
        assert data.options_proceeds == pytest.approx(5000.0)

    def test_zero_amount_row(self):
        """Rows with amount 0 must be included without error."""
        text = (
            "Övriga optioner\n"
            "Interactive Brokers Ireland Limited MES 05SEP25 6610 C 0\n"
            "Interactive Brokers Ireland Limited MES 06AUG25 6350 C 399\n"
            "Summa 399\n"
        )
        data = parse_skv_text(text)
        assert data.options_proceeds == pytest.approx(399.0)

    def test_endash_phrase_with_ib_rows(self):
        """En-dash variant of the phrase should work with real-PDF rows."""
        text = (
            "Övriga terminer – erhållen ersättning\n"
            "Interactive Brokers Ireland Limited MES 19DEC25 (141 st) 27 892\n"
            "Summa 27 892\n"
        )
        data = parse_skv_text(text)
        assert data.futures_proceeds == pytest.approx(27892.0)

    def test_real_pdf_via_mock(self, tmp_path):
        """End-to-end: parse_skv_pdf() with mocked PDF text returns correct values."""
        fake_pdf = tmp_path / "kontroll.pdf"
        fake_pdf.write_bytes(b"%PDF-1.4 fake")

        with patch("skv_parser._extract_text_from_pdf", return_value=_REAL_PDF_TEXT):
            data = parse_skv_pdf(fake_pdf)

        assert data.futures_proceeds == pytest.approx(147649.0)
        assert data.futures_cost == pytest.approx(98558.0)
        assert data.options_proceeds == pytest.approx(6820.0)

    def test_debug_output_produced(self, capsys):
        """debug=True must print [SKV DEBUG] lines to stdout."""
        parse_skv_text(_REAL_PDF_TEXT, debug=True)
        captured = capsys.readouterr()
        assert "[SKV DEBUG]" in captured.out
        assert "Summa" in captured.out or "Row sum" in captured.out
