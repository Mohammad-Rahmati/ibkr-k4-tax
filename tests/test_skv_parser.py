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

        trades = [
            K4Trade(
                date=datetime(2025, 3, 15),
                symbol="ES",
                asset_class="FUTURES",
                quantity=5.0,
                sale_amount_sek=147649.0,
                purchase_amount_sek=98558.0,
                profit_loss_sek=49091.0,
                k4_section="D",
            ),
            K4Trade(
                date=datetime(2025, 6, 10),
                symbol="SPX",
                asset_class="EQUITY AND INDEX OPTIONS",
                quantity=10.0,
                sale_amount_sek=683529.0,
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
