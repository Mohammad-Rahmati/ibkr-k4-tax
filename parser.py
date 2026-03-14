"""
parser.py
---------
Parse IBKR Activity Statement CSV exports.

IBKR Activity Statements are *sectioned* CSV files.  Every row begins with
two mandatory columns:

    <Section>, <Type>, [columns …]

where ``Section`` is the name of the dataset (e.g. ``Trades``) and ``Type``
is one of ``Header``, ``Data``, ``Total``, ``SubTotal``, ``Notes``, etc.

The parser detects each section's header row, then collects all ``Data``
rows for that section into a list of dicts keyed by the header columns.

Real IBKR Activity Statement column layout for Trades
------------------------------------------------------
DataDiscriminator, Asset Category, Currency, Symbol, Date/Time,
Quantity, T. Price, C. Price, Proceeds, Comm/Fee, Basis,
Realized P/L, MTM P/L, Code

Date/Time format: "2025-01-06, 09:50:44"  (quoted, with comma)
Asset Category:   full English names, e.g. "Stocks", "Equity and Index Options"
DataDiscriminator: "Order" for real trades; "SubTotal"/"Total" for summary rows
"""

import csv
import logging
from datetime import datetime
from pathlib import Path
from typing import Generator, IO

from dateutil import parser as dateutil_parser

from config import (
    IBKR_SECTION_COL,
    IBKR_TYPE_COL,
    IBKR_HEADER_ROW_TYPE,
    IBKR_DATA_ROW_TYPE,
    IBKR_TRADES_SECTION,
    IBKR_ORDER_DISCRIMINATOR,
    TRADES_COL_DISCRIMINATOR,
    TRADES_COL_DATETIME,
    TRADES_COL_SYMBOL,
    TRADES_COL_ASSET_CLASS,
    TRADES_COL_QUANTITY,
    TRADES_COL_PRICE,
    TRADES_COL_PROCEEDS,
    TRADES_COL_COMM_FEE,
    TRADES_COL_CURRENCY,
    TRADES_COL_REALIZED_PNL,
)
from models import Trade

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Low-level section reader
# ---------------------------------------------------------------------------

def _iter_sections(
    fileobj: IO[str],
) -> Generator[tuple[str, list[str], list[dict[str, str]]], None, None]:
    """
    Yield ``(section_name, headers, rows)`` tuples for every section found
    in an IBKR Activity Statement CSV stream.

    A section may have *multiple* Header rows (IBKR repeats them for each
    asset-class block).  Each new Header resets the active column mapping
    for subsequent Data rows, so every Data row is keyed against the Header
    that immediately precedes it.

    Parameters
    ----------
    fileobj:
        An open text-mode file-like object positioned at the beginning.

    Yields
    ------
    section_name : str
    headers : list[str]
        Column names (lower-cased, stripped).
    rows : list[dict[str, str]]
        All *Data* rows, keyed by header column name.
    """
    reader = csv.reader(fileobj)

    # section_name → (current_headers, accumulated_data_rows)
    sections: dict[str, tuple[list[str], list[dict[str, str]]]] = {}
    # section_name → headers currently active (may change mid-section)
    active_headers: dict[str, list[str]] = {}

    for raw_row in reader:
        if len(raw_row) < 2:
            continue

        section = raw_row[IBKR_SECTION_COL].strip()
        row_type = raw_row[IBKR_TYPE_COL].strip()
        rest = raw_row[2:]

        if not section:
            continue

        if row_type == IBKR_HEADER_ROW_TYPE:
            headers = [col.strip().lower() for col in rest]
            active_headers[section] = headers
            if section not in sections:
                sections[section] = (headers, [])
            else:
                # Update stored headers to the latest seen (for reference),
                # but keep accumulating into the same data list.
                sections[section] = (headers, sections[section][1])
            logger.debug("Section '%s' header: %s", section, headers)

        elif row_type == IBKR_DATA_ROW_TYPE:
            hdrs = active_headers.get(section)
            if hdrs is None:
                logger.warning(
                    "Data row for section '%s' before its Header; skipping.", section
                )
                continue
            if section not in sections:
                sections[section] = (hdrs, [])
            # Pad so zip never silently drops trailing columns.
            values = rest + [""] * max(0, len(hdrs) - len(rest))
            row_dict = dict(zip(hdrs, values))
            sections[section][1].append(row_dict)

    for section_name, (headers, rows) in sections.items():
        yield section_name, headers, rows


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def parse_activity_statement(path: str | Path) -> dict[str, list[dict[str, str]]]:
    """
    Parse an IBKR Activity Statement CSV file and return all sections.

    Parameters
    ----------
    path:
        Filesystem path to the ``.csv`` file.

    Returns
    -------
    dict mapping section name → list of row dicts (column → value).
    """
    path = Path(path)
    logger.info("Parsing activity statement: %s", path)

    if not path.exists():
        raise FileNotFoundError(f"Activity statement not found: {path}")

    result: dict[str, list[dict[str, str]]] = {}
    with path.open(newline="", encoding="utf-8-sig") as fh:
        for section_name, _headers, rows in _iter_sections(fh):
            result[section_name] = rows
            logger.debug("Section '%s': %d data rows", section_name, len(rows))

    logger.info("Parsed %d sections from %s", len(result), path.name)
    return result


def _normalise_asset_class(raw: str) -> str:
    """
    Return an upper-cased, stripped asset-class string suitable for K4 mapping.

    IBKR uses full English names in Activity Statements (e.g. ``"Stocks"``,
    ``"Equity and Index Options"``).  We normalise to uppercase so the
    K4_SECTION_MAP lookup is case-insensitive.
    """
    return raw.strip().upper()


def _parse_trade_row(row: dict[str, str], row_index: int) -> "Trade | None":
    """
    Convert a raw Trades section data-dict into a :class:`Trade` object.

    Only rows where ``DataDiscriminator == "Order"`` represent actual trades.
    Sub-total and total rows are silently skipped.

    Returns ``None`` if the row should be skipped or cannot be parsed.
    """
    # Skip non-Order rows (SubTotal, Total, etc.)
    discriminator = row.get(TRADES_COL_DISCRIMINATOR, "").strip().lower()
    if discriminator != IBKR_ORDER_DISCRIMINATOR:
        return None

    # Skip rows with empty or header-like quantity
    quantity_raw = row.get(TRADES_COL_QUANTITY, "").strip()
    if not quantity_raw:
        return None

    # Parse date — IBKR format: "2025-01-06, 09:50:44"
    date_raw = row.get(TRADES_COL_DATETIME, "").strip()
    if not date_raw:
        logger.warning("Row %d: empty date field; skipping.", row_index)
        return None

    try:
        trade_date: datetime = dateutil_parser.parse(date_raw)
    except (ValueError, TypeError) as exc:
        logger.warning("Row %d: cannot parse date %r: %s", row_index, date_raw, exc)
        return None

    try:
        # Futures use "notional value" instead of "proceeds"; fall back gracefully.
        proceeds_raw = (
            row.get(TRADES_COL_PROCEEDS, "")
            or row.get("notional value", "")
            or "0"
        ).strip() or "0"
        # Forex uses "comm in usd" instead of "comm/fee"; fall back gracefully.
        fees_raw = (
            row.get(TRADES_COL_COMM_FEE, "")
            or row.get("comm in usd", "")
            or "0"
        ).strip() or "0"
        trade = Trade(
            date=trade_date,
            symbol=row.get(TRADES_COL_SYMBOL, "").strip(),
            asset_class=_normalise_asset_class(row.get(TRADES_COL_ASSET_CLASS, "")),
            quantity=quantity_raw,
            price=row.get(TRADES_COL_PRICE, "0") or "0",
            proceeds=proceeds_raw,
            fees=fees_raw,
            currency=row.get(TRADES_COL_CURRENCY, "").strip(),
            realized_pnl=row.get(TRADES_COL_REALIZED_PNL, "0") or "0",
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("Row %d: validation error: %s | raw=%s", row_index, exc, row)
        return None

    return trade


def extract_trades(
    sections: dict[str, list[dict[str, str]]],
    start: datetime | None = None,
    end: datetime | None = None,
) -> list[Trade]:
    """
    Extract and optionally filter :class:`Trade` objects from parsed sections.

    Parameters
    ----------
    sections:
        Output of :func:`parse_activity_statement`.
    start:
        Inclusive lower bound on trade date (timezone-naive).
    end:
        Inclusive upper bound on trade date (timezone-naive).

    Returns
    -------
    List of :class:`Trade` objects, sorted by date ascending.
    """
    raw_rows = sections.get(IBKR_TRADES_SECTION, [])
    if not raw_rows:
        logger.warning("No '%s' section found in activity statement.", IBKR_TRADES_SECTION)
        return []

    trades: list[Trade] = []
    for idx, row in enumerate(raw_rows):
        trade = _parse_trade_row(row, idx)
        if trade is None:
            continue

        trade_date = trade.date.replace(tzinfo=None)
        if start and trade_date < start.replace(tzinfo=None):
            continue
        if end and trade_date > end.replace(tzinfo=None):
            continue

        trades.append(trade)

    logger.info(
        "Extracted %d trades (from %d raw rows, date filter: %s – %s)",
        len(trades),
        len(raw_rows),
        start.date() if start else "—",
        end.date() if end else "—",
    )
    return sorted(trades, key=lambda t: t.date)


# ---------------------------------------------------------------------------
# Convenience: parse + extract in one call
# ---------------------------------------------------------------------------

def load_trades(
    path: str | Path,
    start: datetime | None = None,
    end: datetime | None = None,
) -> list[Trade]:
    """
    Parse an IBKR Activity Statement and return filtered Trade objects.

    This is the primary entry-point for the pipeline.

    Parameters
    ----------
    path:
        Path to the IBKR ``.csv`` file.
    start:
        Optional inclusive start date filter.
    end:
        Optional inclusive end date filter.

    Returns
    -------
    Sorted list of :class:`Trade` objects.
    """
    sections = parse_activity_statement(path)
    return extract_trades(sections, start=start, end=end)
