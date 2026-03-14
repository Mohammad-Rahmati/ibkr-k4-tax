"""
config.py
---------
Central configuration for ibkr-k4-tax.
All magic constants and environment-driven settings live here.
"""

from pathlib import Path

# ---------------------------------------------------------------------------
# Project paths
# ---------------------------------------------------------------------------
PROJECT_ROOT = Path(__file__).parent
DATA_RAW_DIR = PROJECT_ROOT / "data" / "raw"
DATA_PROCESSED_DIR = PROJECT_ROOT / "data" / "processed"

# ---------------------------------------------------------------------------
# FX / currency settings
# ---------------------------------------------------------------------------
# Sveriges Riksbank SWEA API — free, no API key required.
# Series pattern: SEK{CURRENCY}PMI  (e.g. SEKUSDPMI = SEK per 1 USD)
# Range endpoint: GET /swea/v1/Observations/{series}/{from}/{to}
FX_API_BASE_URL = "https://api.riksbank.se/swea/v1"

# Supported base currencies and their Riksbank series names.
# The value is SEK per 1 unit of the base currency.
FX_RIKSBANK_SERIES: dict[str, str] = {
    "USD": "SEKUSDPMI",
    "EUR": "SEKEURPMI",
    "GBP": "SEKGBPPMI",
    "CHF": "SEKCHFPMI",
    "NOK": "SEKNOKPMI",
    "DKK": "SEKDKKPMI",
    "JPY": "SEKJPYPMI",
    "CAD": "SEKCADPMI",
    "AUD": "SEKAUDPMI",
    "HKD": "SEKHKDPMI",
}

# Local JSON file used to cache FX rate lookups between runs.
FX_CACHE_FILE = PROJECT_ROOT / "data" / "processed" / "fx_cache.json"

# How long a cached FX rate remains valid (days).
# Set to 0 to always re-fetch.
FX_CACHE_TTL_DAYS = 30

# Fallback rate used when the API cannot be reached and no cache exists.
# Set to None to raise an error instead of falling back.
FX_FALLBACK_RATE: float | None = None

# Target reporting currency.
TARGET_CURRENCY = "SEK"

# ---------------------------------------------------------------------------
# K4 section mapping
# asset_class (normalised from IBKR full names) → K4 section
#
# IBKR uses full English names in the "Asset Category" column, e.g.:
#   "Stocks", "Equity and Index Options", "Futures", "Forex", …
# We normalise these to short codes before mapping.
# ---------------------------------------------------------------------------
K4_SECTION_MAP: dict[str, str] = {
    # Short codes (legacy / flex-query exports)
    "STK": "A",
    "ETF": "A",
    "OPT": "D",
    "FOP": "D",
    "FUT": "D",
    "CFD": "D",
    "WAR": "D",
    "BOND": "A",
    # Full names as seen in standard Activity Statement exports
    "STOCKS": "A",
    "EQUITY AND INDEX OPTIONS": "D",
    "FUTURES": "D",
    "FUTURE OPTIONS": "D",
    "FOREX": "D",
    "BONDS": "A",
    "WARRANTS": "D",
    "STRUCTURED PRODUCTS": "D",
    "OPTIONS ON FUTURES": "D",
}

# Default section for unknown asset classes.
K4_DEFAULT_SECTION = "D"

# ---------------------------------------------------------------------------
# IBKR CSV parsing
# ---------------------------------------------------------------------------
# Column index in every CSV row that identifies the section name.
IBKR_SECTION_COL = 0
# Column index that identifies the row type (Header / Data / Total / …).
IBKR_TYPE_COL = 1

IBKR_HEADER_ROW_TYPE = "Header"
IBKR_DATA_ROW_TYPE = "Data"

# Exact section name for trades in the IBKR Activity Statement.
IBKR_TRADES_SECTION = "Trades"

# Value of the DataDiscriminator column that marks individual order rows.
# Sub-total / total rows have a different discriminator and must be skipped.
IBKR_ORDER_DISCRIMINATOR = "order"   # matched case-insensitively

# ---------------------------------------------------------------------------
# Trades section column header names (case-insensitive, stripped).
# IBKR Activity Statements use these exact names.
# ---------------------------------------------------------------------------
TRADES_COL_DISCRIMINATOR = "datadiscriminator"
TRADES_COL_ASSET_CLASS   = "asset category"
TRADES_COL_CURRENCY      = "currency"
TRADES_COL_SYMBOL        = "symbol"
TRADES_COL_DATETIME      = "date/time"
TRADES_COL_QUANTITY      = "quantity"
TRADES_COL_PRICE         = "t. price"
TRADES_COL_PROCEEDS      = "proceeds"
TRADES_COL_COMM_FEE      = "comm/fee"
TRADES_COL_REALIZED_PNL  = "realized p/l"

# ---------------------------------------------------------------------------
# Output file names
# ---------------------------------------------------------------------------
OUTPUT_TRADES_SEK = "trades_sek.csv"
OUTPUT_K4_SUMMARY_CSV = "k4_summary.csv"
OUTPUT_K4_SUMMARY_JSON = "k4_summary.json"

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
LOG_FORMAT = "%(asctime)s  %(levelname)-8s  %(name)s  %(message)s"
LOG_DATE_FORMAT = "%Y-%m-%d %H:%M:%S"
