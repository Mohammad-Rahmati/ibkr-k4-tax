"""
skv_parser.py
-------------
Parse a Skatteverket declaration PDF (Inkomstdeklaration 1 or the separate
*Kontroll- och inkomstuppgifter* attachment) and extract the control totals
that Interactive Brokers pre-fills on behalf of its clients.

Background
----------
Every year IBKR submits summary data to Skatteverket for each Swedish account
holder.  The data appears in the customer's tax declaration under:

  - Övriga terminer – erhållen ersättning   (futures proceeds)
  - Övriga terminer – erlagd ersättning     (futures cost)
  - Övriga optioner                         (options proceeds)

These values can be used to cross-check the totals derived independently from
the IBKR Activity Statement CSV.

Usage
-----
::

    from skv_parser import parse_skv_pdf

    data = parse_skv_pdf(Path("deklaration.pdf"))
    print(data.futures_proceeds)   # e.g. 147649.0

The function returns a :class:`SkvControlData` instance with ``None`` for any
field that could not be found in the PDF.

Design notes
------------
- The PDF is read with **pdfplumber** which handles Swedish characters (å ä ö)
  correctly out of the box.
- Number formats accepted:  ``147 649``, ``147,649``, ``147649``, ``-147649``.
  Swedish PDFs typically use a space as the thousands separator.
- The search is *line-oriented*: we look for each key phrase anywhere on a
  line (case-insensitive, stripped) and then scan forward for the first
  integer-like token on the same or the immediately following lines.
- If pdfplumber cannot open the file an :class:`SkvParseError` is raised.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Phrase → field mapping
# ---------------------------------------------------------------------------

# Maps the Swedish key phrase (lower-cased for matching) to the
# SkvControlData attribute name it populates.
_PHRASE_FIELD: dict[str, str] = {
    "övriga terminer - erhållen ersättning": "futures_proceeds",
    "övriga terminer – erhållen ersättning": "futures_proceeds",
    "övriga terminer -erhållen ersättning": "futures_proceeds",
    "övriga terminer–erhållen ersättning": "futures_proceeds",
    "övriga terminer - erlagd ersättning": "futures_cost",
    "övriga terminer – erlagd ersättning": "futures_cost",
    "övriga terminer -erlagd ersättning": "futures_cost",
    "övriga terminer–erlagd ersättning": "futures_cost",
    "övriga optioner": "options_proceeds",
}

# Ordered list of (normalised-phrase, field) pairs so we can match
# "övriga terminer – erhållen ersättning" before "övriga terminer".
_PHRASE_FIELD_ORDERED: list[tuple[str, str]] = sorted(
    _PHRASE_FIELD.items(), key=lambda kv: len(kv[0]), reverse=True
)

# Regex to extract a (possibly negative, possibly space/comma-separated)
# integer value.  Matches patterns like:
#   147 649    147,649    147649    -98 558    98558
_NUMBER_RE = re.compile(
    r"-?\s*\d[\d\s,]*\d|\d",  # one or more digits possibly with spaces/commas
)

# Tighter pattern to validate that a candidate token is really a number
# (not just whitespace noise).
_DIGIT_RE = re.compile(r"\d")


# ---------------------------------------------------------------------------
# Public data structure
# ---------------------------------------------------------------------------

@dataclass
class SkvControlData:
    """
    Control totals extracted from a Skatteverket declaration PDF.

    All monetary values are in **SEK** (rounded to nearest integer).
    A field is ``None`` when the corresponding phrase was not found.

    Attributes
    ----------
    futures_proceeds:
        "Övriga terminer – erhållen ersättning"
    futures_cost:
        "Övriga terminer – erlagd ersättning"
    options_proceeds:
        "Övriga optioner"
    source_pdf:
        Path to the PDF that was parsed (informational only).
    """

    futures_proceeds: Optional[float] = None
    futures_cost: Optional[float] = None
    options_proceeds: Optional[float] = None
    source_pdf: Optional[Path] = None

    def as_skv_kwargs(self) -> dict[str, float]:
        """
        Return a dict suitable for unpacking into :func:`~reconciliation.compute_reconciliation`.

        Only fields with a value (not ``None``) are included.
        """
        mapping = {
            "skv_futures_proceeds": self.futures_proceeds,
            "skv_futures_cost": self.futures_cost,
            "skv_options_proceeds": self.options_proceeds,
        }
        return {k: v for k, v in mapping.items() if v is not None}

    def any_found(self) -> bool:
        """Return True if at least one field was extracted."""
        return any(
            v is not None
            for v in (self.futures_proceeds, self.futures_cost, self.options_proceeds)
        )

    def __str__(self) -> str:
        parts = []
        if self.futures_proceeds is not None:
            parts.append(f"futures_proceeds={self.futures_proceeds:,.0f}")
        if self.futures_cost is not None:
            parts.append(f"futures_cost={self.futures_cost:,.0f}")
        if self.options_proceeds is not None:
            parts.append(f"options_proceeds={self.options_proceeds:,.0f}")
        return f"SkvControlData({', '.join(parts) or 'empty'})"


# ---------------------------------------------------------------------------
# Custom exception
# ---------------------------------------------------------------------------

class SkvParseError(Exception):
    """Raised when the PDF cannot be opened or read."""


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _normalise_line(line: str) -> str:
    """Lower-case, strip, and collapse multiple whitespace to a single space."""
    return re.sub(r"\s+", " ", line.strip().lower())


def _parse_number(text: str) -> Optional[float]:
    """
    Extract the first numeric value from *text*.

    Handles:
      - Swedish thousands separator (space): ``147 649`` → 147649
      - Comma thousands separator: ``147,649`` → 147649
      - Negative values: ``-98 558`` → -98558
      - Plain integers: ``683529``

    Returns ``None`` when no digit sequence is found.
    """
    # Remove all whitespace inside numbers and commas used as thousands sep,
    # then find an integer pattern.
    # First, condense runs of "digit[space/comma]digit" blocks.
    candidate = re.sub(r"(?<=\d)[\s,](?=\d)", "", text)
    m = re.search(r"-?\d+", candidate)
    if m is None:
        return None
    return float(m.group())


def _extract_value_near_phrase(lines: list[str], phrase_idx: int) -> Optional[float]:
    """
    Look for the first numeric value on the phrase line (after the phrase)
    or on up to 3 following lines.

    Parameters
    ----------
    lines:
        All lines from the page (already normalised for matching purposes,
        but we need the original for number extraction — pass originals here).
    phrase_idx:
        Index of the line that contains the matching phrase.
    """
    # Try same line first (value might appear after the label on the same line).
    same_line = lines[phrase_idx]
    # Strip the matched phrase portion by taking everything after the colon
    # or after the last word of the phrase.  Simpler: look for a number anywhere
    # after the first digit.
    value = _parse_number(same_line)
    if value is not None:
        # Sanity check: a valid SEK total will be non-zero and positive
        # (cost is also positive in Skatteverket's representation).
        # We allow 0 only if the line clearly has a number.
        if _DIGIT_RE.search(same_line):
            return value

    # Scan up to 3 following lines.
    for offset in range(1, 4):
        idx = phrase_idx + offset
        if idx >= len(lines):
            break
        candidate = lines[idx].strip()
        if not candidate:
            continue
        # Stop if we hit another key phrase (don't bleed into the next section).
        norm_candidate = _normalise_line(candidate)
        if any(phrase in norm_candidate for phrase, _ in _PHRASE_FIELD_ORDERED):
            break
        value = _parse_number(candidate)
        if value is not None:
            return value

    return None


def _extract_text_from_pdf(pdf_path: Path) -> str:
    """
    Open the PDF with pdfplumber and concatenate text from all pages.

    Raises
    ------
    SkvParseError
        If the file cannot be opened.
    """
    try:
        import pdfplumber  # local import keeps the module importable when not installed
    except ImportError as exc:
        raise SkvParseError(
            "pdfplumber is not installed.  Run: pip install pdfplumber"
        ) from exc

    try:
        with pdfplumber.open(pdf_path) as pdf:
            page_texts = []
            for page in pdf.pages:
                text = page.extract_text() or ""
                page_texts.append(text)
            return "\n".join(page_texts)
    except Exception as exc:
        raise SkvParseError(f"Cannot read PDF '{pdf_path}': {exc}") from exc


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def parse_skv_pdf(pdf_path: Path) -> SkvControlData:
    """
    Parse a Skatteverket declaration PDF and extract IBKR control totals.

    Parameters
    ----------
    pdf_path:
        Path to the PDF file (must exist).

    Returns
    -------
    :class:`SkvControlData`
        Extracted values; missing fields are ``None``.

    Raises
    ------
    FileNotFoundError
        If ``pdf_path`` does not exist.
    SkvParseError
        If pdfplumber cannot open the file.
    """
    pdf_path = Path(pdf_path)
    if not pdf_path.exists():
        raise FileNotFoundError(f"PDF not found: {pdf_path}")

    raw_text = _extract_text_from_pdf(pdf_path)
    return parse_skv_text(raw_text, source_pdf=pdf_path)


def parse_skv_text(text: str, *, source_pdf: Optional[Path] = None) -> SkvControlData:
    """
    Parse already-extracted PDF text and return :class:`SkvControlData`.

    This function accepts raw text (e.g. from :func:`_extract_text_from_pdf`)
    so it can be called directly in tests without needing an actual PDF.

    Parameters
    ----------
    text:
        Full text content of the PDF (all pages concatenated).
    source_pdf:
        Optional path to the source PDF, stored in the result for reference.

    Returns
    -------
    :class:`SkvControlData`
    """
    lines = text.splitlines()
    norm_lines = [_normalise_line(line) for line in lines]

    result: dict[str, Optional[float]] = {
        "futures_proceeds": None,
        "futures_cost": None,
        "options_proceeds": None,
    }

    # Track which fields have been populated so we don't overwrite with a
    # second match (e.g. "Övriga optioner" might appear on multiple pages).
    found: set[str] = set()

    for i, norm_line in enumerate(norm_lines):
        if not norm_line:
            continue

        for phrase, field in _PHRASE_FIELD_ORDERED:
            if field in found:
                continue
            if phrase not in norm_line:
                continue

            # Matched.  Now extract the numeric value from this line and
            # nearby following lines.  We pass the original (non-normalised)
            # lines so number extraction sees the real digits.
            value = _extract_value_near_phrase(lines, i)

            if value is not None:
                result[field] = value
                found.add(field)
                logger.debug(
                    "Extracted %s = %.0f  (line %d: %r)",
                    field, value, i + 1, lines[i][:80],
                )
            else:
                logger.warning(
                    "Phrase found but no numeric value detected near line %d: %r",
                    i + 1, lines[i][:80],
                )

            # Only match the first (longest) phrase per line — skip remaining
            # phrases once one matches.
            break

    data = SkvControlData(
        futures_proceeds=result["futures_proceeds"],
        futures_cost=result["futures_cost"],
        options_proceeds=result["options_proceeds"],
        source_pdf=source_pdf,
    )

    if not data.any_found():
        logger.warning(
            "No Skatteverket control totals found in the PDF text. "
            "The PDF may use a different format or may not contain IBKR data."
        )
    else:
        logger.info("Parsed Skatteverket PDF: %s", data)

    return data
