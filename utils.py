"""
utils.py
--------
Shared utility functions used across ibkr-k4-tax modules.

Provides:
- Logging configuration
- Date parsing helpers
- Safe numeric conversion
- Path helpers
"""

import logging
import sys
from datetime import date, datetime
from pathlib import Path
from typing import Optional

from dateutil import parser as dateutil_parser

from config import LOG_FORMAT, LOG_DATE_FORMAT


# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

def configure_logging(level: str = "INFO", log_file: Optional[Path] = None) -> None:
    """
    Configure the root logger for the application.

    Parameters
    ----------
    level:
        Logging level string: ``"DEBUG"``, ``"INFO"``, ``"WARNING"``, etc.
    log_file:
        Optional path to a log file.  If supplied, output is written to both
        the console and the file.
    """
    numeric_level = getattr(logging, level.upper(), logging.INFO)
    handlers: list[logging.Handler] = [logging.StreamHandler(sys.stdout)]

    if log_file is not None:
        log_file = Path(log_file)
        log_file.parent.mkdir(parents=True, exist_ok=True)
        handlers.append(logging.FileHandler(log_file, encoding="utf-8"))

    logging.basicConfig(
        level=numeric_level,
        format=LOG_FORMAT,
        datefmt=LOG_DATE_FORMAT,
        handlers=handlers,
        force=True,
    )


# ---------------------------------------------------------------------------
# Date helpers
# ---------------------------------------------------------------------------

def parse_date(value: str, param_name: str = "date") -> datetime:
    """
    Parse a user-supplied date string into a :class:`~datetime.datetime`.

    Accepts many common formats (ISO-8601, ``YYYY-MM-DD``, etc.) via
    ``python-dateutil``.

    Parameters
    ----------
    value:
        Raw date string to parse.
    param_name:
        Name of the parameter (used in error messages).

    Returns
    -------
    :class:`datetime.datetime`

    Raises
    ------
    ValueError
        If the string cannot be parsed.
    """
    try:
        return dateutil_parser.parse(value)
    except (ValueError, TypeError) as exc:
        raise ValueError(
            f"Cannot parse {param_name!r} value {value!r}: {exc}"
        ) from exc


def to_date(value: date | datetime | str) -> date:
    """
    Coerce a value to a :class:`~datetime.date`.

    Parameters
    ----------
    value:
        A ``date``, ``datetime``, or date string.

    Returns
    -------
    :class:`datetime.date`
    """
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    return parse_date(str(value)).date()


# ---------------------------------------------------------------------------
# Numeric helpers
# ---------------------------------------------------------------------------

def safe_float(value: object, default: float = 0.0) -> float:
    """
    Convert *value* to float, returning *default* on failure.

    Handles string values with commas (e.g. ``"1,234.56"``).

    Parameters
    ----------
    value:
        Value to convert.
    default:
        Fallback when conversion fails.

    Returns
    -------
    float
    """
    if value is None or value == "":
        return default
    try:
        cleaned = str(value).replace(",", "").strip()
        return float(cleaned)
    except (ValueError, TypeError):
        return default


def round_sek(amount: float, decimals: int = 2) -> float:
    """
    Round a SEK amount to the specified number of decimal places.

    Parameters
    ----------
    amount:
        Value to round.
    decimals:
        Number of decimal places (default: 2).

    Returns
    -------
    float
    """
    return round(amount, decimals)


# ---------------------------------------------------------------------------
# Path helpers
# ---------------------------------------------------------------------------

def ensure_dir(path: str | Path) -> Path:
    """
    Create a directory (and all parents) if it does not exist.

    Parameters
    ----------
    path:
        Directory path to create.

    Returns
    -------
    :class:`~pathlib.Path`
        The resolved directory path.
    """
    p = Path(path)
    p.mkdir(parents=True, exist_ok=True)
    return p


def resolve_output_dir(output: str | Path | None, default: str | Path = "./output") -> Path:
    """
    Return the resolved output directory path.

    If *output* is ``None``, falls back to *default*.

    Parameters
    ----------
    output:
        User-supplied output path (may be ``None``).
    default:
        Fallback directory.

    Returns
    -------
    :class:`~pathlib.Path`
    """
    p = Path(output) if output else Path(default)
    return ensure_dir(p)
