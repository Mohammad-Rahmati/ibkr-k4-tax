"""
reconciliation.py
-----------------
Validate K4 trade totals against the control data that Interactive Brokers
reports to Skatteverket (the Swedish Tax Agency).

Background
----------
IBKR sends two categories of pre-filled data to Skatteverket each year:

1. **Terminer** (futures)
   - "Övriga terminer – erhållen ersättning"  → total proceeds
   - "Övriga terminer – erlagd ersättning"     → total cost

2. **Optioner** (options)
   - "Övriga optioner"                         → total proceeds only

The numbers appear in the user's Inkomstdeklaration 1 (K4 annex) and can be
used to cross-check the totals derived from the IBKR Activity Statement CSV.

This module:
- Computes per-section (A / D) and per-instrument-type totals from the
  already-converted :class:`~models.K4Trade` list.
- Optionally accepts Skatteverket control figures supplied by the user.
- Flags deviations above a configurable tolerance threshold.
- Renders a human-readable reconciliation report.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from config import (
    FUTURES_ASSET_CLASSES,
    OPTIONS_ASSET_CLASSES,
    OUTPUT_RECONCILIATION_REPORT,
    RECONCILIATION_TOLERANCE,
)
from models import K4Trade

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------

@dataclass
class DerivativeBreakdown:
    """
    Aggregated totals for a single derivative type (futures or options)
    expressed in SEK.
    """

    asset_type: str          # human label, e.g. "Futures" or "Options"
    proceeds: float = 0.0    # sum of sale_amount_sek
    cost: float = 0.0        # sum of purchase_amount_sek
    profit: float = 0.0
    loss: float = 0.0
    trade_count: int = 0


@dataclass
class SectionTotals:
    """Aggregated totals for one K4 section (A or D)."""

    section: str             # "A" or "D"
    proceeds: float = 0.0
    cost: float = 0.0
    profit: float = 0.0
    loss: float = 0.0
    trade_count: int = 0

    @property
    def net(self) -> float:
        return round(self.profit - self.loss, 2)


@dataclass
class ControlDiff:
    """
    Comparison between a calculated value and a Skatteverket control figure.

    Attributes
    ----------
    label:
        Human-readable description of the figure being compared.
    calculated:
        Value derived from the IBKR CSV.
    skv:
        Value reported to Skatteverket by the broker.
    tolerance:
        Fractional tolerance (e.g. 0.05 for 5 %).
    """

    label: str
    calculated: float
    skv: float
    tolerance: float = RECONCILIATION_TOLERANCE

    @property
    def difference(self) -> float:
        return round(self.calculated - self.skv, 2)

    @property
    def deviation_pct(self) -> float:
        """Relative deviation as a fraction (0–1).  Returns 0.0 if skv == 0."""
        if self.skv == 0:
            return 0.0 if self.calculated == 0 else 1.0
        return abs(self.difference) / abs(self.skv)

    @property
    def ok(self) -> bool:
        return self.deviation_pct <= self.tolerance


@dataclass
class ReconciliationResult:
    """
    Full result of a reconciliation run.

    Attributes
    ----------
    trade_count:
        Total number of K4Trade objects analysed.
    section_a:
        Aggregated totals for K4 Section A (stocks / ETFs / bonds).
    section_d:
        Aggregated totals for K4 Section D (derivatives).
    futures:
        Breakdown for futures and future-related instruments.
    options:
        Breakdown for equity and index options.
    diffs:
        List of comparisons against Skatteverket control figures (may be
        empty if no control figures were provided).
    tolerance:
        Fractional tolerance used for status determination.
    """

    trade_count: int
    section_a: SectionTotals
    section_d: SectionTotals
    futures: DerivativeBreakdown
    options: DerivativeBreakdown
    diffs: list[ControlDiff] = field(default_factory=list)
    tolerance: float = RECONCILIATION_TOLERANCE

    @property
    def all_ok(self) -> bool:
        """True if all comparisons are within tolerance (or none were made)."""
        return all(d.ok for d in self.diffs)

    @property
    def status_label(self) -> str:
        if not self.diffs:
            return "No Skatteverket data provided"
        tolerance_pct = round(self.tolerance * 100)
        if self.all_ok:
            return f"OK (within {tolerance_pct}% tolerance)"
        failing = [d.label for d in self.diffs if not d.ok]
        return f"WARNING – deviation exceeds {tolerance_pct}% for: {', '.join(failing)}"


# ---------------------------------------------------------------------------
# Core computation
# ---------------------------------------------------------------------------

def compute_reconciliation(
    k4_trades: list[K4Trade],
    *,
    skv_futures_proceeds: float | None = None,
    skv_futures_cost: float | None = None,
    skv_options_proceeds: float | None = None,
    tolerance: float = RECONCILIATION_TOLERANCE,
) -> ReconciliationResult:
    """
    Compute reconciliation totals from a list of :class:`~models.K4Trade` objects.

    Parameters
    ----------
    k4_trades:
        SEK-converted trade objects produced by the K4 generation pipeline.
    skv_futures_proceeds:
        Skatteverket control figure for futures proceeds (optional).
    skv_futures_cost:
        Skatteverket control figure for futures cost (optional).
    skv_options_proceeds:
        Skatteverket control figure for options proceeds (optional).
    tolerance:
        Maximum acceptable relative deviation before a check is flagged.

    Returns
    -------
    :class:`ReconciliationResult`
    """
    sec_a = SectionTotals(section="A")
    sec_d = SectionTotals(section="D")
    futures = DerivativeBreakdown(asset_type="Futures")
    options = DerivativeBreakdown(asset_type="Options")

    for t in k4_trades:
        asset_upper = t.asset_class.upper().strip()

        # --- Section totals ---
        target_sec = sec_a if t.k4_section == "A" else sec_d
        target_sec.proceeds += t.sale_amount_sek
        target_sec.cost += t.purchase_amount_sek
        if t.profit_loss_sek >= 0:
            target_sec.profit += t.profit_loss_sek
        else:
            target_sec.loss += abs(t.profit_loss_sek)
        target_sec.trade_count += 1

        # --- Derivative type breakdown ---
        if asset_upper in FUTURES_ASSET_CLASSES:
            futures.proceeds += t.sale_amount_sek
            futures.cost += t.purchase_amount_sek
            if t.profit_loss_sek >= 0:
                futures.profit += t.profit_loss_sek
            else:
                futures.loss += abs(t.profit_loss_sek)
            futures.trade_count += 1

        elif asset_upper in OPTIONS_ASSET_CLASSES:
            options.proceeds += t.sale_amount_sek
            options.cost += t.purchase_amount_sek
            if t.profit_loss_sek >= 0:
                options.profit += t.profit_loss_sek
            else:
                options.loss += abs(t.profit_loss_sek)
            options.trade_count += 1

    # Round all float fields
    for obj in (sec_a, sec_d, futures, options):
        for attr in ("proceeds", "cost", "profit", "loss"):
            setattr(obj, attr, round(getattr(obj, attr), 2))

    # --- Skatteverket comparisons ---
    diffs: list[ControlDiff] = []

    if skv_futures_proceeds is not None:
        diffs.append(
            ControlDiff(
                label="Futures proceeds",
                calculated=futures.proceeds,
                skv=skv_futures_proceeds,
                tolerance=tolerance,
            )
        )

    if skv_futures_cost is not None:
        diffs.append(
            ControlDiff(
                label="Futures cost",
                calculated=futures.cost,
                skv=skv_futures_cost,
                tolerance=tolerance,
            )
        )

    if skv_options_proceeds is not None:
        diffs.append(
            ControlDiff(
                label="Options proceeds",
                calculated=options.proceeds,
                skv=skv_options_proceeds,
                tolerance=tolerance,
            )
        )

    result = ReconciliationResult(
        trade_count=len(k4_trades),
        section_a=sec_a,
        section_d=sec_d,
        futures=futures,
        options=options,
        diffs=diffs,
        tolerance=tolerance,
    )

    logger.info(
        "Reconciliation complete.  Status: %s  |  Trades: %d  |  "
        "Sec-A profit=%.0f  |  Sec-D net=%.0f",
        result.status_label,
        result.trade_count,
        sec_a.profit,
        sec_d.net,
    )

    return result


# ---------------------------------------------------------------------------
# Report rendering
# ---------------------------------------------------------------------------

def format_report(result: ReconciliationResult) -> str:
    """
    Render a :class:`ReconciliationResult` as a human-readable text report.

    Parameters
    ----------
    result:
        Output of :func:`compute_reconciliation`.

    Returns
    -------
    Formatted multi-line string.
    """

    def _fmt(value: float) -> str:
        """Format a float as a rounded integer with thousands separator."""
        return f"{round(value):,}"

    def _fmt_diff(value: float) -> str:
        sign = "+" if value >= 0 else ""
        return f"{sign}{round(value):,}"

    lines: list[str] = []
    hr = "-" * 40

    lines.append("IBKR K4 RECONCILIATION REPORT")
    lines.append(hr)
    lines.append("")
    lines.append(f"Trades parsed: {result.trade_count:,}")
    lines.append("")

    # Section A
    lines.append("SECTION A  (Stocks / ETFs / Bonds)")
    lines.append(f"  Trades:    {result.section_a.trade_count:,}")
    lines.append(f"  Proceeds:  {_fmt(result.section_a.proceeds)} SEK")
    lines.append(f"  Cost:      {_fmt(result.section_a.cost)} SEK")
    lines.append(f"  Profit:    {_fmt(result.section_a.profit)} SEK")
    lines.append(f"  Loss:      {_fmt(result.section_a.loss)} SEK")
    lines.append(f"  Net:       {_fmt(result.section_a.net)} SEK")
    lines.append("")

    # Section D
    lines.append("SECTION D  (Derivatives / Forex)")
    lines.append(f"  Trades:    {result.section_d.trade_count:,}")
    lines.append(f"  Proceeds:  {_fmt(result.section_d.proceeds)} SEK")
    lines.append(f"  Cost:      {_fmt(result.section_d.cost)} SEK")
    lines.append(f"  Profit:    {_fmt(result.section_d.profit)} SEK")
    lines.append(f"  Loss:      {_fmt(result.section_d.loss)} SEK")
    lines.append(f"  Net:       {_fmt(result.section_d.net)} SEK")
    lines.append("")

    # Derivatives breakdown
    lines.append("DERIVATIVES BREAKDOWN")
    for breakdown in (result.futures, result.options):
        lines.append(f"  {breakdown.asset_type}")
        lines.append(f"    Trades:   {breakdown.trade_count:,}")
        lines.append(f"    Proceeds: {_fmt(breakdown.proceeds)} SEK")
        lines.append(f"    Cost:     {_fmt(breakdown.cost)} SEK")
        lines.append(f"    Profit:   {_fmt(breakdown.profit)} SEK")
        lines.append(f"    Loss:     {_fmt(breakdown.loss)} SEK")
    lines.append("")

    # Skatteverket control data (only when provided)
    if result.diffs:
        lines.append("SKATTEVERKET CONTROL DATA")
        for d in result.diffs:
            lines.append(f"  {d.label:<28} {_fmt(d.skv)} SEK")
        lines.append("")

        lines.append("DIFFERENCE  (calculated − Skatteverket)")
        for d in result.diffs:
            pct = round(d.deviation_pct * 100, 1)
            flag = "" if d.ok else "  ⚠  EXCEEDS TOLERANCE"
            lines.append(
                f"  {d.label:<28} {_fmt_diff(d.difference)} SEK"
                f"  ({pct}%){flag}"
            )
        lines.append("")

    lines.append(f"Status: {result.status_label}")
    lines.append("")

    return "\n".join(lines)


def write_report(result: ReconciliationResult, output_dir: Path) -> Path:
    """
    Write the reconciliation report to *output_dir/reconciliation_report.txt*.

    Parameters
    ----------
    result:
        Output of :func:`compute_reconciliation`.
    output_dir:
        Destination directory (created if necessary).

    Returns
    -------
    Path to the written file.
    """
    output_dir.mkdir(parents=True, exist_ok=True)
    out_path = output_dir / OUTPUT_RECONCILIATION_REPORT
    report_text = format_report(result)
    out_path.write_text(report_text, encoding="utf-8")
    logger.info("Wrote reconciliation report: %s", out_path)
    return out_path
