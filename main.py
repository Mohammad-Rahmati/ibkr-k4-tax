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
from k4_generator import generate_k4_report

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

    # 5. Summary
    print()
    print("=" * 60)
    print("  ibkr-k4-tax  —  Output files")
    print("=" * 60)
    for name, path in output_paths.items():
        print(f"  {name:<30}  {path}")
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
