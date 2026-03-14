"""
k4_generator.py
---------------
Convert raw :class:`~models.Trade` objects into Swedish K4 tax output.

Pipeline
~~~~~~~~
1. Determine K4 section for each trade (A or D).
2. Fetch the appropriate FX rate for the trade date and currency.
3. Convert proceeds and cost basis to SEK.
4. Calculate profit/loss in SEK.
5. Aggregate per-symbol and per-section summaries.
6. Export three output files:
   - ``trades_sek.csv``
   - ``k4_summary.csv``
   - ``k4_summary.json``
"""

import json
import logging
from collections import defaultdict
from datetime import date, datetime
from pathlib import Path
from typing import Optional

import pandas as pd

from config import (
    K4_SECTION_MAP,
    K4_DEFAULT_SECTION,
    OUTPUT_TRADES_SEK,
    OUTPUT_K4_SUMMARY_CSV,
    OUTPUT_K4_SUMMARY_JSON,
    TARGET_CURRENCY,
)
from fx import FXRateProvider
from models import K4SectionSummary, K4SymbolSummary, K4Trade, Trade

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Section classification
# ---------------------------------------------------------------------------

def classify_k4_section(asset_class: str) -> str:
    """
    Return the K4 section (``"A"`` or ``"D"``) for the given IBKR asset class.

    Accepts both short codes (``"STK"``) and full names
    (``"Stocks"``, ``"Equity and Index Options"``).  Input is normalised to
    upper-case before lookup so the match is always case-insensitive.

    Parameters
    ----------
    asset_class:
        IBKR asset-class string (e.g. ``"STK"``, ``"OPT"``).

    Returns
    -------
    str
        ``"A"`` or ``"D"``.
    """
    section = K4_SECTION_MAP.get(asset_class.upper().strip(), K4_DEFAULT_SECTION)
    logger.debug("Asset class '%s' → K4 section %s", asset_class, section)
    return section


# ---------------------------------------------------------------------------
# SEK conversion
# ---------------------------------------------------------------------------

def convert_trade_to_sek(
    trade: Trade,
    fx_provider: FXRateProvider,
) -> K4Trade:
    """
    Convert a single :class:`Trade` to a :class:`K4Trade` with SEK amounts.

    The cost basis is estimated as ``proceeds - realized_pnl`` (i.e. what
    IBKR reports as the book value of the position at close).

    Parameters
    ----------
    trade:
        Raw normalised trade.
    fx_provider:
        Provider used to look up the historical FX rate.

    Returns
    -------
    :class:`K4Trade`
    """
    trade_date = trade.date.date() if isinstance(trade.date, datetime) else trade.date

    # Fetch rate for the trade's currency → SEK
    rate = fx_provider.get_rate(trade_date, base=trade.currency, target=TARGET_CURRENCY)

    # IBKR Proceeds are negative for purchases and positive for sales.
    # For K4 we always report:
    #   sale_amount   = absolute proceeds (what you received)
    #   purchase_amount = absolute cost basis (what you paid)
    # The sign of realized_pnl determines profit vs. loss.
    proceeds_sek = trade.proceeds * rate
    # cost_basis = proceeds - pnl  (algebraic: what you paid)
    cost_basis = trade.proceeds - trade.realized_pnl
    cost_basis_sek = cost_basis * rate
    profit_loss_sek = proceeds_sek - cost_basis_sek

    k4_section = classify_k4_section(trade.asset_class)

    return K4Trade(
        date=trade.date,
        symbol=trade.symbol,
        asset_class=trade.asset_class,
        quantity=trade.quantity,
        sale_amount_sek=round(proceeds_sek, 2),
        purchase_amount_sek=round(cost_basis_sek, 2),
        profit_loss_sek=round(profit_loss_sek, 2),
        k4_section=k4_section,
    )


def convert_trades_to_sek(
    trades: list[Trade],
    fx_provider: FXRateProvider,
) -> list[K4Trade]:
    """
    Convert a list of :class:`Trade` objects to :class:`K4Trade` objects.

    Rates are pre-fetched in bulk (one API call per unique date/currency pair)
    to minimise latency.

    Parameters
    ----------
    trades:
        List of raw normalised trades.
    fx_provider:
        FX rate provider.

    Returns
    -------
    List of :class:`K4Trade` objects.
    """
    if not trades:
        logger.warning("No trades to convert.")
        return []

    # Pre-fetch rates grouped by currency — one bulk API call per currency
    # covers all trade dates, minimising API round-trips and avoiding rate limits.
    currency_dates: dict[str, list[date]] = defaultdict(list)
    for t in trades:
        d = t.date.date() if isinstance(t.date, datetime) else t.date
        currency_dates[t.currency.upper()].append(d)

    total_pairs = sum(len(set(ds)) for ds in currency_dates.values())
    logger.info("Pre-fetching FX rates for %d unique date/currency pairs …", total_pairs)
    for currency, dates in sorted(currency_dates.items()):
        if currency != TARGET_CURRENCY:
            fx_provider.prefetch_rates(dates, base=currency, target=TARGET_CURRENCY)

    k4_trades: list[K4Trade] = []
    errors = 0
    for trade in trades:
        try:
            k4_trades.append(convert_trade_to_sek(trade, fx_provider))
        except Exception as exc:  # noqa: BLE001
            logger.error(
                "Could not convert trade %s %s on %s: %s",
                trade.symbol,
                trade.asset_class,
                trade.date.date(),
                exc,
            )
            errors += 1

    logger.info(
        "Converted %d/%d trades to SEK (%d errors).",
        len(k4_trades),
        len(trades),
        errors,
    )
    return k4_trades


# ---------------------------------------------------------------------------
# Aggregation
# ---------------------------------------------------------------------------

def build_symbol_summaries(k4_trades: list[K4Trade]) -> list[K4SymbolSummary]:
    """
    Aggregate :class:`K4Trade` objects into per-symbol summaries.

    Parameters
    ----------
    k4_trades:
        List of SEK-converted trades.

    Returns
    -------
    List of :class:`K4SymbolSummary` objects sorted by section then symbol.
    """
    buckets: dict[tuple[str, str], K4SymbolSummary] = {}

    for t in k4_trades:
        key = (t.symbol, t.k4_section)
        if key not in buckets:
            buckets[key] = K4SymbolSummary(symbol=t.symbol, k4_section=t.k4_section)
        s = buckets[key]
        s.total_sales_sek += t.sale_amount_sek
        s.total_cost_sek += t.purchase_amount_sek
        if t.profit_loss_sek >= 0:
            s.profit_sek += t.profit_loss_sek
        else:
            s.loss_sek += abs(t.profit_loss_sek)

    # Round all totals
    for s in buckets.values():
        s.total_sales_sek = round(s.total_sales_sek, 2)
        s.total_cost_sek = round(s.total_cost_sek, 2)
        s.profit_sek = round(s.profit_sek, 2)
        s.loss_sek = round(s.loss_sek, 2)

    return sorted(buckets.values(), key=lambda x: (x.k4_section, x.symbol))


def build_section_summaries(
    symbol_summaries: list[K4SymbolSummary],
) -> dict[str, K4SectionSummary]:
    """
    Aggregate per-symbol summaries into per-section totals.

    Parameters
    ----------
    symbol_summaries:
        Output of :func:`build_symbol_summaries`.

    Returns
    -------
    Dict mapping ``"section_a"`` / ``"section_d"`` → :class:`K4SectionSummary`.
    """
    result: dict[str, K4SectionSummary] = {
        "section_a": K4SectionSummary(),
        "section_d": K4SectionSummary(),
    }

    for sym in symbol_summaries:
        key = f"section_{sym.k4_section.lower()}"
        if key not in result:
            continue
        sec = result[key]
        sec.total_sales += sym.total_sales_sek
        sec.total_cost += sym.total_cost_sek
        sec.profit += sym.profit_sek
        sec.loss += sym.loss_sek

    for sec in result.values():
        sec.total_sales = round(sec.total_sales, 2)
        sec.total_cost = round(sec.total_cost, 2)
        sec.profit = round(sec.profit, 2)
        sec.loss = round(sec.loss, 2)

    return result


# ---------------------------------------------------------------------------
# Export helpers
# ---------------------------------------------------------------------------

def _ensure_output_dir(output_dir: Path) -> None:
    output_dir.mkdir(parents=True, exist_ok=True)


def export_trades_sek(k4_trades: list[K4Trade], output_dir: Path) -> Path:
    """
    Write ``trades_sek.csv`` to *output_dir*.

    Parameters
    ----------
    k4_trades:
        SEK-converted trades.
    output_dir:
        Destination directory.

    Returns
    -------
    Path to the written file.
    """
    _ensure_output_dir(output_dir)
    out_path = output_dir / OUTPUT_TRADES_SEK

    rows = [
        {
            "date": t.date.strftime("%Y-%m-%d"),
            "symbol": t.symbol,
            "asset_class": t.asset_class,
            "quantity": t.quantity,
            "sale_amount_sek": t.sale_amount_sek,
            "purchase_amount_sek": t.purchase_amount_sek,
            "profit_loss_sek": t.profit_loss_sek,
            "k4_section": t.k4_section,
        }
        for t in k4_trades
    ]

    df = pd.DataFrame(rows)
    df.to_csv(out_path, index=False)
    logger.info("Wrote %s (%d rows)", out_path, len(df))
    return out_path


def export_k4_summary_csv(
    symbol_summaries: list[K4SymbolSummary], output_dir: Path
) -> Path:
    """
    Write ``k4_summary.csv`` to *output_dir*.

    Parameters
    ----------
    symbol_summaries:
        Per-symbol aggregated summaries.
    output_dir:
        Destination directory.

    Returns
    -------
    Path to the written file.
    """
    _ensure_output_dir(output_dir)
    out_path = output_dir / OUTPUT_K4_SUMMARY_CSV

    rows = [
        {
            "symbol": s.symbol,
            "k4_section": s.k4_section,
            "total_sales_sek": s.total_sales_sek,
            "total_cost_sek": s.total_cost_sek,
            "profit_sek": s.profit_sek,
            "loss_sek": s.loss_sek,
        }
        for s in symbol_summaries
    ]

    df = pd.DataFrame(rows)
    df.to_csv(out_path, index=False)
    logger.info("Wrote %s (%d symbols)", out_path, len(df))
    return out_path


def export_k4_summary_json(
    section_summaries: dict[str, K4SectionSummary], output_dir: Path
) -> Path:
    """
    Write ``k4_summary.json`` to *output_dir*.

    Parameters
    ----------
    section_summaries:
        Output of :func:`build_section_summaries`.
    output_dir:
        Destination directory.

    Returns
    -------
    Path to the written file.
    """
    _ensure_output_dir(output_dir)
    out_path = output_dir / OUTPUT_K4_SUMMARY_JSON

    payload = {
        key: {
            "total_sales": sec.total_sales,
            "total_cost": sec.total_cost,
            "profit": sec.profit,
            "loss": sec.loss,
        }
        for key, sec in section_summaries.items()
    }

    with out_path.open("w", encoding="utf-8") as fh:
        json.dump(payload, fh, indent=2)

    logger.info("Wrote %s", out_path)
    return out_path


# ---------------------------------------------------------------------------
# Façade: run the full K4 generation pipeline
# ---------------------------------------------------------------------------

def generate_k4_report(
    trades: list[Trade],
    output_dir: Path,
    fx_provider: FXRateProvider | None = None,
) -> dict[str, Path]:
    """
    Run the full K4 generation pipeline and write all output files.

    Parameters
    ----------
    trades:
        Normalised trade objects from the parser.
    output_dir:
        Directory where output files will be written.
    fx_provider:
        Optional custom FX rate provider.  A default instance is created if
        not supplied.

    Returns
    -------
    Dict mapping output file name → absolute Path.
    """
    if fx_provider is None:
        fx_provider = FXRateProvider()

    # Step 1: Convert to SEK
    k4_trades = convert_trades_to_sek(trades, fx_provider)

    # Step 2: Build summaries
    symbol_summaries = build_symbol_summaries(k4_trades)
    section_summaries = build_section_summaries(symbol_summaries)

    # Step 3: Export
    paths: dict[str, Path] = {}
    paths[OUTPUT_TRADES_SEK] = export_trades_sek(k4_trades, output_dir)
    paths[OUTPUT_K4_SUMMARY_CSV] = export_k4_summary_csv(symbol_summaries, output_dir)
    paths[OUTPUT_K4_SUMMARY_JSON] = export_k4_summary_json(section_summaries, output_dir)

    logger.info(
        "K4 report complete.  Section A profit=%.2f loss=%.2f | Section D profit=%.2f loss=%.2f",
        section_summaries["section_a"].profit,
        section_summaries["section_a"].loss,
        section_summaries["section_d"].profit,
        section_summaries["section_d"].loss,
    )

    return paths
