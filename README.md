# ibkr-k4-tax

Convert **Interactive Brokers Activity Statement CSV** exports into a
**Swedish capital gains tax report** ready for Skatteverket **K4** filing.

---

## Table of Contents

1. [Overview](#overview)
2. [IBKR CSV Format](#ibkr-csv-format)
3. [Swedish K4 Reporting](#swedish-k4-reporting)
4. [Installation](#installation)
5. [Usage](#usage)
6. [Output Files](#output-files)
7. [Project Structure](#project-structure)
8. [Running Tests](#running-tests)
9. [Configuration](#configuration)

---

## Overview

Swedish tax residents who trade through Interactive Brokers must declare
capital gains and losses on **Skatteverket form K4**.  This tool automates
the tedious manual work:

- Parses the sectioned CSV format that IBKR uses for Activity Statements.
- Extracts all closed trades within a user-specified date range.
- Fetches historical exchange rates from **exchangerate.host** and converts
  all amounts to **SEK**.
- Categorises each instrument into the correct K4 section (A or D).
- Produces three output files: a per-trade CSV, a per-symbol summary CSV,
  and a machine-readable JSON summary.

---

## IBKR CSV Format

IBKR Activity Statements are *sectioned* CSV files.  Every row starts with
two header columns that identify the section and row type:

```
Section,Type,Col1,Col2,...
```

| Column   | Description                           |
|----------|---------------------------------------|
| Section  | Dataset name (e.g. `Trades`)          |
| Type     | `Header`, `Data`, `Total`, `SubTotal` |

### Example

```csv
Statement,Header,Field Name,Field Value
Statement,Data,BrokerName,Interactive Brokers Ireland Limited

Trades,Header,DateTime,Symbol,Asset Category,Quantity,T. Price,Proceeds,Comm/Fee,Currency,Realized P/L
Trades,Data,2025-01-03 14:32:10,QQQ,OPT,1,2.35,235,-1.25,USD,100
```

The parser reads the `Header` row to discover column names, then collects
every `Data` row for each section into a list of dictionaries.

Only the **Trades** section is required for K4 calculations.

---

## Swedish K4 Reporting

K4 is the Swedish capital gains declaration form.  The form has two relevant
sections for retail investors:

| Section | Covers                           |
|---------|----------------------------------|
| **A**   | Stocks, ETFs, bonds              |
| **D**   | Options, futures, warrants, CFDs |

### Asset-class mapping

| IBKR Asset Class | K4 Section |
|------------------|------------|
| `STK`            | A          |
| `ETF`            | A          |
| `OPT`            | D          |
| `FOP`            | D          |
| `FUT`            | D          |
| `CFD`            | D          |

### Currency conversion

All amounts must be reported in SEK.  The tool uses the **official trade date
rate** fetched from `exchangerate.host`:

```
sale_amount_sek      = proceeds_usd   × usdsek_rate
purchase_amount_sek  = cost_basis_usd × usdsek_rate
profit_loss_sek      = sale_amount_sek − purchase_amount_sek
```

---

## Installation

### 1. Install Miniconda

If you don't have Miniconda (or Anaconda) installed, download and install it
from the official page:

```
https://docs.conda.io/en/latest/miniconda.html
```

Verify the installation:

```bash
conda --version
```

### 2. Clone the repository

```bash
git clone https://github.com/Mohammad-Rahmati/ibkr-k4-tax.git
cd ibkr-k4-tax
```

### 3. Create the conda environment

```bash
conda create -n ibkr-k4-tax python=3.12 -y
```

### 4. Activate the environment

```bash
conda activate ibkr-k4-tax
```

> You will need to run this command every time you open a new terminal.
> Your prompt should change to show `(ibkr-k4-tax)`.

### 5. Install dependencies

```bash
pip install -r requirements.txt
```

---

## Usage

```bash
python main.py \
  --input  data/raw/activity.csv \
  --start  2025-01-01 \
  --end    2025-12-31 \
  --output ./output
```

### All options

| Flag          | Required | Default    | Description                             |
|---------------|----------|------------|-----------------------------------------|
| `--input`     | ✓        | —          | Path to the IBKR Activity Statement CSV |
| `--start`     | ✓        | —          | Inclusive start date (`YYYY-MM-DD`)     |
| `--end`       | ✓        | —          | Inclusive end date   (`YYYY-MM-DD`)     |
| `--output`    |          | `./output` | Directory to write output files         |
| `--log-level` |          | `INFO`     | `DEBUG` / `INFO` / `WARNING` / `ERROR`  |
| `--log-file`  |          | —          | Optional path to a log file             |
| `--fx-cache`  |          | —          | Override the FX cache file location     |

---

## Output Files

### `trades_sek.csv`

One row per closed trade, all amounts in SEK.

| Column                | Description                     |
|-----------------------|---------------------------------|
| `date`                | Trade date                      |
| `symbol`              | Ticker symbol                   |
| `asset_class`         | IBKR asset class (STK, OPT, …)  |
| `quantity`            | Number of units traded          |
| `sale_amount_sek`     | Proceeds converted to SEK       |
| `purchase_amount_sek` | Cost basis converted to SEK     |
| `profit_loss_sek`     | Net profit or loss in SEK       |
| `k4_section`          | K4 section: `A` or `D`         |

### `k4_summary.csv`

Aggregated per symbol and K4 section — one row per symbol.

| Column            | Description                      |
|-------------------|----------------------------------|
| `symbol`          | Ticker                           |
| `k4_section`      | `A` or `D`                       |
| `total_sales_sek` | Sum of all sale amounts          |
| `total_cost_sek`  | Sum of all purchase amounts      |
| `profit_sek`      | Sum of profitable trades         |
| `loss_sek`        | Sum of loss-making trades (abs)  |

### `k4_summary.json`

Machine-readable section totals:

```json
{
  "section_a": {
    "total_sales": 125000.00,
    "total_cost":  118000.00,
    "profit":        9000.00,
    "loss":          2000.00
  },
  "section_d": {
    "total_sales":  15000.00,
    "total_cost":   12500.00,
    "profit":        3000.00,
    "loss":           500.00
  }
}
```

---

## Project Structure

```
ibkr-k4-tax/
├── main.py            # CLI entry point & pipeline orchestration
├── parser.py          # IBKR CSV parser
├── fx.py              # Historical FX rates with caching
├── k4_generator.py    # SEK conversion, K4 categorisation, export
├── models.py          # Pydantic data models (Trade, K4Trade, …)
├── utils.py           # Logging, date helpers, numeric utilities
├── config.py          # All configuration constants
│
├── tests/
│   ├── test_parser.py
│   ├── test_fx.py
│   └── test_k4.py
│
├── data/
│   ├── raw/           # Place IBKR CSV exports here (git-ignored)
│   └── processed/     # Generated outputs & FX cache (git-ignored)
│
├── requirements.txt
└── README.md
```

---

## Running Tests

Make sure the conda environment is active first:

```bash
conda activate ibkr-k4-tax
```

```bash
# Run all tests
pytest

# With coverage report
pytest --cov=. --cov-report=term-missing

# Verbose output
pytest -v
```

---

## Configuration

All constants are centralised in `config.py`.  Key settings:

| Constant            | Default                         | Description                                   |
|---------------------|---------------------------------|-----------------------------------------------|
| `FX_API_BASE_URL`   | `https://api.exchangerate.host` | Exchange rate API base URL                    |
| `FX_CACHE_FILE`     | `data/processed/fx_cache.json`  | Local FX rate cache                           |
| `FX_CACHE_TTL_DAYS` | `30`                            | Cache validity in days (0 = always re-fetch)  |
| `FX_FALLBACK_RATE`  | `None`                          | Rate when API unreachable (`None` = error)    |
| `TARGET_CURRENCY`   | `SEK`                           | Reporting currency                            |
| `K4_SECTION_MAP`    | See `config.py`                 | Asset class → K4 section mapping              |

---

## License

MIT
