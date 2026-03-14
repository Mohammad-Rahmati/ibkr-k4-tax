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

Real PDF format (Kontroll- och inkomstuppgifter)
------------------------------------------------
Each section in the real Skatteverket PDF looks like::

    Övriga terminer - erhållen ersättning
    Interactive Brokers Ireland Limited MES 19DEC25 (141 st) 27 892
    Interactive Brokers Ireland Limited MES 19SEP25 (187 st) 24 226
    ...
    Summa 147 649

    Övriga terminer - erlagd ersättning
    Interactive Brokers Ireland Limited MES 20MAR26 (15 st) 4 130
    ...
    Summa 98 558

Key parsing rules:
- Each data row starts with ``Interactive Brokers``.
- Numbers inside parentheses such as ``(141 st)`` are **ignored**
  because they are quantities, not SEK amounts.
- The **last number** on each data row is the SEK amount.
- A ``Summa XXXXX`` line ends the section and gives the authoritative total.
  We prefer this over summing rows (handles rounding/cases we might miss).
- The row-summing approach is the primary path; ``Summa`` is cross-checked.

Legacy single-value format (synthetic tests / older PDFs)
----------------------------------------------------------
The parser also handles the simpler format where a single value appears on
the line immediately after the phrase::

    Övriga terminer - erhållen ersättning
    147 649

In this case the value is extracted directly from the following line.

Design notes
------------
- The PDF is read with **pdfplumber** which handles Swedish characters (å ä ö)
  correctly out of the box.
- Number formats accepted:  ``147 649``, ``147,649``, ``147649``, ``-147649``.
  Swedish PDFs typically use a space as the thousands separator.
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
# "övriga terminer – erhållen ersättning" before "övriga optioner".
_PHRASE_FIELD_ORDERED: list[tuple[str, str]] = sorted(
    _PHRASE_FIELD.items(), key=lambda kv: len(kv[0]), reverse=True
)

# Regex: strip parenthesised groups like "(141 st)" before number extraction.
_PAREN_RE = re.compile(r"\([^)]*\)")

# Regex: match the last space-delimited digit token (with optional internal
# spaces for thousands separators) at the end of a line, after removing
# parenthesised content.  The lookbehind ``(?<=\s)`` ensures the number
# starts after a whitespace character (not mid-token like "19DEC25"), while
# the alternation ``^(\d...)`` handles a line that is purely numeric.
# Captures e.g. "27 892" or "147649" or "89".
_LAST_NUMBER_RE = re.compile(r"(?:(?<=\s)|^)(\d[\d ]*\d|\d)$")

# Regex: whole-line "Summa <number>" — the authoritative section total.
_SUMMA_RE = re.compile(r"^\s*summa\s+([\d\s,]+)", re.IGNORECASE)

# Digit guard used when checking for any number in a string.
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
    Extract the **first** numeric value from *text* (legacy helper, kept for
    backward compatibility with tests and the simple single-value-next-line
    format).

    Handles:
      - Swedish thousands separator (space): ``147 649`` → 147649
      - Comma thousands separator: ``147,649`` → 147649
      - Negative values: ``-98 558`` → -98558
      - Plain integers: ``683529``

    Returns ``None`` when no digit sequence is found.
    """
    candidate = re.sub(r"(?<=\d)[\s,](?=\d)", "", text)
    m = re.search(r"-?\d+", candidate)
    if m is None:
        return None
    return float(m.group())


def _extract_last_number(line: str) -> Optional[int]:
    """
    Extract the **last** SEK amount from a data row.

    Algorithm
    ---------
    1. Strip any parenthesised groups (e.g. ``(141 st)``, ``(2 st)``).
    2. Match the trailing digit run using ``r'(\\d[\\d\\s]*)$'``.
    3. Remove all internal spaces (``"27 892"`` → ``27892``).
    4. Return as ``int``, or ``None`` if no trailing number exists.

    Examples
    --------
    >>> _extract_last_number("Interactive Brokers Ireland Limited MES 19DEC25 (141 st) 27 892")
    27892
    >>> _extract_last_number("Interactive Brokers Ireland Limited VXM 21JAN26 (31 st) 1 093")
    1093
    >>> _extract_last_number("Interactive Brokers Ireland Limited MES 05SEP25 6610 C 0")
    0
    >>> _extract_last_number("Summa 147 649")
    147649
    """
    # Step 1: remove parenthesised content
    stripped = _PAREN_RE.sub("", line).rstrip()
    # Step 2: match the last digit run at end of (stripped) line
    m = _LAST_NUMBER_RE.search(stripped)
    if m is None:
        return None
    # Step 3: remove internal spaces and convert
    number_str = m.group(1).replace(" ", "").replace(",", "")
    if not number_str:
        return None
    return int(number_str)


def _is_section_header(norm_line: str) -> bool:
    """Return True if *norm_line* matches any known section phrase."""
    return any(phrase in norm_line for phrase, _ in _PHRASE_FIELD_ORDERED)


def _parse_number_after_phrase(original_line: str, norm_line: str) -> Optional[float]:
    """
    Return the number that appears on the *same* line as a section phrase,
    i.e. after the label text (e.g. ``"Övriga optioner  683 529"``).
    """
    for phrase, _ in _PHRASE_FIELD_ORDERED:
        idx = norm_line.find(phrase)
        if idx == -1:
            continue
        suffix = original_line[idx + len(phrase):]
        if _DIGIT_RE.search(suffix):
            return _parse_number(suffix)
        return None
    return None


def _debug_print(
    phrase_line: str,
    data_rows: list[tuple[str, int]],
    summa_value: Optional[int],
) -> None:
    """Print debug information for a collected section."""
    print(f"\n[SKV DEBUG] Section: {phrase_line.strip()!r}")
    if data_rows:
        print(f"  Rows collected: {len(data_rows)}")
        for raw, val in data_rows:
            print(f"    {raw.strip()!r}  →  {val}")
        row_sum = sum(v for _, v in data_rows)
        print(f"  Row sum: {row_sum}")
    else:
        print("  (no Interactive Brokers data rows found)")
    if summa_value is not None:
        print(f"  Summa line value: {summa_value}")
    final = summa_value if summa_value is not None else (
        sum(v for _, v in data_rows) if data_rows else "N/A"
    )
    print(f"  → Final value used: {final}")


def _collect_section(
    lines: list[str],
    norm_lines: list[str],
    start_idx: int,
    *,
    debug: bool = False,
) -> Optional[float]:
    """
    Collect SEK amounts from a section that starts at *start_idx*.

    Two formats are supported:

    **Real-PDF format** (``Interactive Brokers`` data rows + ``Summa`` line)::

        Övriga terminer - erhållen ersättning   ← start_idx
        Interactive Brokers Ireland Limited MES 19DEC25 (141 st) 27 892
        Interactive Brokers Ireland Limited MES 19SEP25 (187 st) 24 226
        Summa 147 649

    When a ``Summa`` line is found we return its value directly (it is the
    authoritative total pre-computed by Skatteverket).  The row sums are
    printed for debugging but not used as the final value.

    **Legacy / simple format** (value on same line or next line)::

        Övriga terminer - erhållen ersättning  147 649
        — or —
        Övriga terminer - erhållen ersättning
        147 649

    Parameters
    ----------
    lines:
        Original (non-normalised) lines.
    norm_lines:
        Lower-cased, collapsed-whitespace versions of *lines*.
    start_idx:
        Index of the line that contains the matching section phrase.
    debug:
        When ``True``, print each collected row and its extracted value.

    Returns
    -------
    float or ``None``
    """
    phrase_line = lines[start_idx]
    n = len(lines)

    # Check for a value on the same line as the phrase (legacy format).
    same_line_value = _parse_number_after_phrase(phrase_line, norm_lines[start_idx])

    # -----------------------------------------------------------------------
    # Pass 1: scan forward to collect data.
    # We determine the format lazily: if we see an "Interactive Brokers" row
    # we are in real-PDF mode and will collect all such rows + the Summa line.
    # If we never see any IB rows, we fall back to legacy format.
    # -----------------------------------------------------------------------
    data_rows: list[tuple[str, int]] = []
    summa_value: Optional[int] = None

    for i in range(start_idx + 1, n):
        raw = lines[i]
        norm = norm_lines[i]

        if not norm:
            continue

        # Stop at the next section header.
        if _is_section_header(norm):
            break

        # "Summa XXXXX" → authoritative section total
        m = _SUMMA_RE.match(raw)
        if m:
            val = _extract_last_number(raw)
            if val is not None:
                summa_value = val
            break

        # Data row (Interactive Brokers line)
        if norm.startswith("interactive brokers"):
            val = _extract_last_number(raw)
            if val is not None:
                data_rows.append((raw, val))
            continue

        # All other lines (page headers, form numbers, etc.) are ignored when
        # we are in real-PDF mode (i.e. IB rows have been or will be seen).
        # We never try to parse them as values here; the legacy path below
        # handles the case where NO IB rows were found at all.

    # -----------------------------------------------------------------------
    # Pass 2: decide the result
    # -----------------------------------------------------------------------
    if debug:
        _debug_print(phrase_line, data_rows, summa_value)

    if summa_value is not None:
        # Real-PDF format: Summa line is authoritative.
        return float(summa_value)

    if data_rows:
        # Real-PDF format without a Summa line: sum the collected rows.
        total = sum(v for _, v in data_rows)
        if debug:
            print(f"  [SKV DEBUG] Summa line not found — using row sum: {total}")
        return float(total)

    # -----------------------------------------------------------------------
    # Legacy / simple format: no IB rows were found.
    # Accept a value on the same line, or on the immediately following lines
    # (up to 3), skipping blank lines.  Only accept lines that are purely
    # numeric (no alphabetic content) to avoid false positives from page
    # headers and form numbers.
    # -----------------------------------------------------------------------
    if same_line_value is not None:
        if debug:
            print(f"  [SKV DEBUG] Same-line value: {same_line_value:.0f}")
        return same_line_value

    for i in range(start_idx + 1, min(start_idx + 6, n)):
        raw = lines[i]
        norm = norm_lines[i]
        if not norm:
            continue
        if _is_section_header(norm):
            break
        # Only accept a purely-numeric line (digits, spaces, commas, optional minus)
        stripped_non_digit = re.sub(r"[\d\s,]", "", raw.strip())
        if stripped_non_digit in ("", "-") and _DIGIT_RE.search(norm):
            val = _parse_number(raw)
            if val is not None:
                if debug:
                    print(f"  [SKV DEBUG] Legacy next-line value: {raw!r} → {val:.0f}")
                return val

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

def parse_skv_pdf(pdf_path: Path, *, debug: bool = False) -> SkvControlData:
    """
    Parse a Skatteverket declaration PDF and extract IBKR control totals.

    Parameters
    ----------
    pdf_path:
        Path to the PDF file (must exist).
    debug:
        When ``True``, print extracted lines and values for each section.

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
    return parse_skv_text(raw_text, source_pdf=pdf_path, debug=debug)


def parse_skv_text(
    text: str,
    *,
    source_pdf: Optional[Path] = None,
    debug: bool = False,
) -> SkvControlData:
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
    debug:
        When ``True``, print extracted lines and values for each section to
        stdout.  Useful for diagnosing parsing failures.

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

            # Matched.  Collect the section value using the new multi-row logic.
            value = _collect_section(lines, norm_lines, i, debug=debug)

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
