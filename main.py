"""
main.py
-------
Command-line entry point for ibkr-k4-tax.

Usage
-----
    python main.py --input activity.csv --start 2025-01-01 --end 2025-12-31

Run ``python main.py --help`` for the full option list.
"""

import argparse
import logging
import sys
from pathlib import Path

from utils import configure_logging, parse_date, resolve_output_dir
from parser import load_trades
from fx import FXRateProvider
from k4_generator import generate_k4_report, convert_trades_to_sek, build_symbol_summaries, build_section_summaries
from reconciliation import compute_reconciliation, write_report, format_report
from skv_parser import parse_skv_pdf, SkvControlData, SkvParseError

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# CLI definition
# ---------------------------------------------------------------------------

def build_arg_parser() -> argparse.ArgumentParser:
    """Build and return the argument parser."""
    p = argparse.ArgumentParser(
        prog="ibkr-k4-tax",
        description=(
            "Convert an Interactive Brokers Activity Statement CSV export "
            "into a Swedish capital gains tax report (Skatteverket K4)."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Full year 2025
  python main.py --input activity.csv --start 2025-01-01 --end 2025-12-31

  # Specify a custom output directory
  python main.py --input activity.csv --start 2025-01-01 --end 2025-12-31 --output ./reports/2025

  # With Skatteverket PDF (auto-extracts control totals)
  python main.py --input activity.csv --start 2025-01-01 --end 2025-12-31 \\
    --tax-pdf deklaration.pdf

  # With manually entered Skatteverket figures (fallback if no PDF)
  python main.py --input activity.csv --start 2025-01-01 --end 2025-12-31 \\
    --skv-futures-proceeds 147649 --skv-futures-cost 98558 --skv-options-proceeds 683529

  # Debug logging
  python main.py --input activity.csv --start 2025-01-01 --end 2025-12-31 --log-level DEBUG
        """,
    )

    p.add_argument(
        "--input", "-i",
        required=True,
        metavar="FILE",
        help="Path to the IBKR Activity Statement CSV file.",
    )
    p.add_argument(
        "--start", "-s",
        required=True,
        metavar="DATE",
        help="Start date for trade filter (inclusive), e.g. 2025-01-01.",
    )
    p.add_argument(
        "--end", "-e",
        required=True,
        metavar="DATE",
        help="End date for trade filter (inclusive), e.g. 2025-12-31.",
    )
    p.add_argument(
        "--output", "-o",
        default="./output",
        metavar="DIR",
        help="Directory where output files are written (default: ./output).",
    )
    p.add_argument(
        "--log-level",
        default="INFO",
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
        help="Logging verbosity (default: INFO).",
    )
    p.add_argument(
        "--log-file",
        default=None,
        metavar="FILE",
        help="Optional path to a log file (output is also printed to stdout).",
    )
    p.add_argument(
        "--fx-cache",
        default=None,
        metavar="FILE",
        help="Override the default FX cache file location.",
    )

    # --- Skatteverket reconciliation (all optional) ---
    skv = p.add_argument_group(
        "Skatteverket reconciliation",
        "Provide your Skatteverket declaration PDF (--tax-pdf) or enter the "
        "control figures manually (--skv-* flags).  The PDF takes priority; "
        "manual flags are used as fallback when no PDF is given.",
    )
    skv.add_argument(
        "--tax-pdf",
        default=None,
        metavar="FILE",
        help=(
            "Path to the Skatteverket declaration PDF (Inkomstdeklaration 1 "
            "or Kontroll- och inkomstuppgifter).  The parser automatically "
            "extracts 'Övriga terminer' and 'Övriga optioner' control totals."
        ),
    )
    skv.add_argument(
        "--skv-futures-proceeds",
        type=float,
        default=None,
        metavar="SEK",
        help=(
            "Skatteverket control figure: "
            "'Övriga terminer – erhållen ersättning' (futures proceeds, SEK)."
        ),
    )
    skv.add_argument(
        "--skv-futures-cost",
        type=float,
        default=None,
        metavar="SEK",
        help=(
            "Skatteverket control figure: "
            "'Övriga terminer – erlagd ersättning' (futures cost, SEK)."
        ),
    )
    skv.add_argument(
        "--skv-options-proceeds",
        type=float,
        default=None,
        metavar="SEK",
        help=(
            "Skatteverket control figure: "
            "'Övriga optioner' (options proceeds, SEK)."
        ),
    )
    skv.add_argument(
        "--reconciliation-tolerance",
        type=float,
        default=0.05,
        metavar="FRACTION",
        help=(
            "Maximum acceptable relative deviation before a reconciliation "
            "check is flagged as a warning (default: 0.05 = 5%%)."
        ),
    )

    return p


# ---------------------------------------------------------------------------
# Pipeline orchestration
# ---------------------------------------------------------------------------

def run(args: argparse.Namespace) -> int:
    """
    Execute the full pipeline and return an exit code (0 = success).

    Parameters
    ----------
    args:
        Parsed CLI arguments.

    Returns
    -------
    int
        Exit code suitable for ``sys.exit()``.
    """
    # 1. Parse dates
    try:
        start_dt = parse_date(args.start, param_name="--start")
        end_dt = parse_date(args.end, param_name="--end")
    except ValueError as exc:
        logger.error("Date parsing error: %s", exc)
        return 1

    if start_dt > end_dt:
        logger.error("--start (%s) must not be after --end (%s).", args.start, args.end)
        return 1

    input_path = Path(args.input)
    if not input_path.exists():
        logger.error("Input file not found: %s", input_path)
        return 1

    output_dir = resolve_output_dir(args.output)
    logger.info("Output directory: %s", output_dir.resolve())

    # 2. Parse activity statement
    logger.info("Loading trades from: %s", input_path.resolve())
    trades = load_trades(input_path, start=start_dt, end=end_dt)

    if not trades:
        logger.warning(
            "No trades found in %s for the date range %s – %s.",
            input_path.name,
            start_dt.date(),
            end_dt.date(),
        )
        # Still generate empty output files so the pipeline is idempotent.

    logger.info("Found %d trades.", len(trades))

    # 3. Build FX provider
    fx_kwargs: dict = {}
    if args.fx_cache:
        fx_kwargs["cache_file"] = Path(args.fx_cache)
    fx_provider = FXRateProvider(**fx_kwargs)

    # 4. Generate K4 report
    output_paths = generate_k4_report(trades, output_dir, fx_provider=fx_provider)

    # 5. Resolve Skatteverket control figures
    #    Priority: --tax-pdf > --skv-* manual flags
    skv_kwargs: dict = {}

    if args.tax_pdf:
        tax_pdf_path = Path(args.tax_pdf)
        if not tax_pdf_path.exists():
            logger.error("Tax PDF not found: %s", tax_pdf_path)
            return 1
        try:
            skv_data: SkvControlData = parse_skv_pdf(tax_pdf_path)
        except SkvParseError as exc:
            logger.error("Failed to parse Skatteverket PDF: %s", exc)
            return 1

        skv_kwargs = skv_data.as_skv_kwargs()

        if not skv_data.any_found():
            logger.warning(
                "No IBKR control totals found in %s.  "
                "Reconciliation will proceed without Skatteverket figures.",
                tax_pdf_path.name,
            )
        else:
            logger.info("Skatteverket PDF parsed: %s", skv_data)

    else:
        # Manual fallback: use --skv-* flags if provided
        if args.skv_futures_proceeds is not None:
            skv_kwargs["skv_futures_proceeds"] = args.skv_futures_proceeds
        if args.skv_futures_cost is not None:
            skv_kwargs["skv_futures_cost"] = args.skv_futures_cost
        if args.skv_options_proceeds is not None:
            skv_kwargs["skv_options_proceeds"] = args.skv_options_proceeds

    # 6. Reconciliation
    #    generate_k4_report already converted trades internally; we need the
    #    K4Trade list to feed into reconciliation.  Re-use the same fx_provider
    #    (rates are fully cached after step 4, so no extra API calls).
    k4_trades = convert_trades_to_sek(trades, fx_provider)
    recon = compute_reconciliation(
        k4_trades,
        tolerance=args.reconciliation_tolerance,
        **skv_kwargs,
    )
    recon_path = write_report(recon, output_dir)
    output_paths["reconciliation_report.txt"] = recon_path

    # 7. Summary
    sec_a = recon.section_a
    sec_d = recon.section_d

    print()
    print("=" * 60)
    print("  ibkr-k4-tax  —  Summary")
    print("=" * 60)
    print(f"  Trades processed:       {recon.trade_count:,}")
    print(f"  Section A profit:       {sec_a.profit:>12,.0f} SEK")
    print(f"  Section A loss:         {sec_a.loss:>12,.0f} SEK")
    print(f"  Section D profit:       {sec_d.profit:>12,.0f} SEK")
    print(f"  Section D loss:         {sec_d.loss:>12,.0f} SEK")
    print(f"  Section D net profit:   {sec_d.net:>12,.0f} SEK")
    print()
    if recon.diffs:
        src = f" (from {Path(args.tax_pdf).name})" if args.tax_pdf else ""
        print(f"  Skatteverket validation{src}: {recon.status_label}")
    else:
        print("  Reconciliation: no Skatteverket data provided")
    print()
    print("=" * 60)
    print("  Output files")
    print("=" * 60)
    for name, path in output_paths.items():
        print(f"  {name:<34}  {path}")
    print("=" * 60)

    return 0


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> None:
    """Parse arguments, configure logging, and run the pipeline."""
    arg_parser = build_arg_parser()
    args = arg_parser.parse_args()

    configure_logging(
        level=args.log_level,
        log_file=Path(args.log_file) if args.log_file else None,
    )

    logger.debug("Arguments: %s", vars(args))

    sys.exit(run(args))


if __name__ == "__main__":
    main()
