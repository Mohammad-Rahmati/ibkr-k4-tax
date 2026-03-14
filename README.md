# ibkr-k4-tax

Convert an **Interactive Brokers Activity Statement CSV** export into a
**Swedish capital gains tax report (Skatteverket K4)** — and automatically
validate the calculated totals against the control data that IBKR reports to
Skatteverket each year.

---

## Table of Contents

1. [Overview](#overview)
2. [IBKR CSV Format](#ibkr-csv-format)
3. [Swedish K4 Reporting](#swedish-k4-reporting)
4. [Installation](#installation)
5. [Usage](#usage)
6. [Output Files](#output-files)
7. [Skatteverket Reconciliation](#skatteverket-reconciliation)
8. [Project Structure](#project-structure)
9. [Running Tests](#running-tests)
10. [Configuration](#configuration)

---

## Overview

Swedish tax residents who trade through Interactive Brokers must declare
capital gains and losses on **Skatteverket form K4**.  This tool automates
the tedious manual work:

- Parses the sectioned CSV format used by IBKR Activity Statements.
- Extracts all closed trades within a user-specified date range.
- Fetches historical exchange rates from the **Sveriges Riksbank SWEA API**
  (free, no API key required) and converts all amounts to **SEK**.
- Categorises each instrument into the correct K4 section (A or D).
- Produces a per-trade CSV, a per-symbol summary CSV, and a JSON summary.
- **Optionally validates** the computed totals against the control figures
  that IBKR pre-fills in the user's *Inkomstdeklaration 1* — either by
  parsing the declaration PDF automatically or by entering the figures
  manually on the command line.

---

## IBKR CSV Format

IBKR Activity Statements are *sectioned* CSV files.  Every row begins with
two header columns identifying the section and row type:

```
Section,Type,Col1,Col2,...
```

| Column  | Description                           |
|---------|---------------------------------------|
| Section | Dataset name (e.g. `Trades`)          |
| Type    | `Header`, `Data`, `Total`, `SubTotal` |

### Example

```csv
Trades,Header,DataDiscriminator,Asset Category,Currency,Symbol,Date/Time,Quantity,T. Price,Proceeds,Comm/Fee,Realized P/L
Trades,Data,Order,Equity and Index Options,USD,SPX,2025-06-10 14:32:10,10,135.25,135250,-12.50,18300
```

The parser reads the `Header` row to discover column names, then collects
every `Data` row with `DataDiscriminator = Order` (skipping sub-total rows).

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

| IBKR Asset Class                    | K4 Section |
|-------------------------------------|------------|
| Stocks / `STK` / `ETF`              | A          |
| Equity and Index Options / `OPT`    | D          |
| Futures / `FUT`                     | D          |
| Future Options / `FOP`              | D          |
| Options on Futures                  | D          |
| Forex / CFD / Warrants              | D          |

### Currency conversion

All amounts must be reported in SEK.  The tool fetches **official daily
mid-rates** from the Sveriges Riksbank SWEA API:

```
sale_amount_sek      = proceeds_usd   × usdsek_rate
purchase_amount_sek  = cost_basis_usd × usdsek_rate
profit_loss_sek      = sale_amount_sek − purchase_amount_sek
```

Rates are fetched in bulk (one API call per currency per year range) and
cached locally in `data/processed/fx_cache.json` for 30 days.

---

## Installation

### 1. Install Miniconda

```bash
# Download from https://docs.conda.io/en/latest/miniconda.html
conda --version   # verify
```

### 2. Clone the repository

```bash
git clone https://github.com/Mohammad-Rahmati/ibkr-k4-tax.git
cd ibkr-k4-tax
```

### 3. Create and activate the conda environment

```bash
conda create -n ibkr-k4-tax python=3.12 -y
conda activate ibkr-k4-tax
```

> Your prompt should change to `(ibkr-k4-tax)`.  Repeat `conda activate
> ibkr-k4-tax` every time you open a new terminal.

### 4. Install dependencies

```bash
pip install -r requirements.txt
```

---

## Usage

### Basic

```bash
python main.py \
  --input  data/raw/activity.csv \
  --start  2025-01-01 \
  --end    2025-12-31
```

### With Skatteverket PDF (recommended)

If you have your *Inkomstdeklaration 1* or *Kontroll- och inkomstuppgifter*
PDF from Skatteverket, pass it with `--tax-pdf`.  The parser automatically
extracts the IBKR control totals and runs the reconciliation check:

```bash
python main.py \
  --input   data/raw/activity.csv \
  --start   2025-01-01 \
  --end     2025-12-31 \
  --tax-pdf data/raw/deklaration.pdf
```

### With manually entered figures (fallback)

If you don't have the PDF, you can type the values directly from the
declaration:

```bash
python main.py \
  --input                  data/raw/activity.csv \
  --start                  2025-01-01 \
  --end                    2025-12-31 \
  --skv-futures-proceeds   147649 \
  --skv-futures-cost       98558  \
  --skv-options-proceeds   683529
```

### All options

| Flag                          | Required | Default    | Description                                           |
|-------------------------------|----------|------------|-------------------------------------------------------|
| `--input` / `-i`              | ✓        | —          | IBKR Activity Statement CSV                           |
| `--start` / `-s`              | ✓        | —          | Inclusive start date (`YYYY-MM-DD`)                   |
| `--end` / `-e`                | ✓        | —          | Inclusive end date (`YYYY-MM-DD`)                     |
| `--output` / `-o`             |          | `./output` | Directory to write output files                       |
| `--log-level`                 |          | `INFO`     | `DEBUG` / `INFO` / `WARNING` / `ERROR`                |
| `--log-file`                  |          | —          | Optional path to a log file                           |
| `--fx-cache`                  |          | —          | Override the FX cache file location                   |
| `--tax-pdf`                   |          | —          | Skatteverket declaration PDF (auto-extracts totals)   |
| `--skv-futures-proceeds`      |          | —          | Manual: *Övriga terminer – erhållen ersättning* (SEK) |
| `--skv-futures-cost`          |          | —          | Manual: *Övriga terminer – erlagd ersättning* (SEK)   |
| `--skv-options-proceeds`      |          | —          | Manual: *Övriga optioner* (SEK)                       |
| `--reconciliation-tolerance`  |          | `0.05`     | Max acceptable relative deviation before WARNING      |

> `--tax-pdf` takes priority over the manual `--skv-*` flags.

### Example output

```
============================================================
  ibkr-k4-tax  —  Summary
============================================================
  Trades processed:              1,685
  Section A profit:             20,300 SEK
  Section A loss:                    0 SEK
  Section D profit:            344,044 SEK
  Section D loss:              199,706 SEK
  Section D net profit:        144,338 SEK

  Skatteverket validation (from deklaration.pdf): OK (within 5% tolerance)

============================================================
  Output files
============================================================
  trades_sek.csv                    output/trades_sek.csv
  k4_summary.csv                    output/k4_summary.csv
  k4_summary.json                   output/k4_summary.json
  reconciliation_report.txt         output/reconciliation_report.txt
============================================================
```

---

## Output Files

### `trades_sek.csv`

One row per closed trade, all amounts in SEK.

| Column                | Description                     |
|-----------------------|---------------------------------|
| `date`                | Trade date                      |
| `symbol`              | Ticker symbol                   |
| `asset_class`         | IBKR asset class                |
| `quantity`            | Number of units traded          |
| `sale_amount_sek`     | Proceeds converted to SEK       |
| `purchase_amount_sek` | Cost basis converted to SEK     |
| `profit_loss_sek`     | Net profit or loss in SEK       |
| `k4_section`          | `A` or `D`                      |

### `k4_summary.csv`

Aggregated per symbol and K4 section — one row per symbol.

| Column            | Description                     |
|-------------------|---------------------------------|
| `symbol`          | Ticker                          |
| `k4_section`      | `A` or `D`                      |
| `total_sales_sek` | Sum of all sale amounts         |
| `total_cost_sek`  | Sum of all purchase amounts     |
| `profit_sek`      | Sum of profitable trades        |
| `loss_sek`        | Sum of loss-making trades (abs) |

### `k4_summary.json`

Machine-readable section totals:

```json
{
  "section_a": {
    "total_sales": 317786.76,
    "total_cost":  297486.66,
    "profit":       20300.10,
    "loss":             0.00
  },
  "section_d": {
    "total_sales":  831250.00,
    "total_cost":   686912.00,
    "profit":       344044.00,
    "loss":         199706.00
  }
}
```

### `reconciliation_report.txt`

Human-readable validation report (always written, even without SKV figures):

```
IBKR K4 RECONCILIATION REPORT
----------------------------------------

Trades parsed: 1,685

SECTION A  (Stocks / ETFs / Bonds)
  Trades:    75
  Proceeds:  317,787 SEK
  ...
  Net:       20,300 SEK

SECTION D  (Derivatives / Forex)
  Trades:    1,610
  Proceeds:  831,250 SEK
  ...

DERIVATIVES BREAKDOWN
  Futures
    Proceeds: 148,100 SEK
    Cost:      98,450 SEK
  Options
    Proceeds: 683,000 SEK
    ...

SKATTEVERKET CONTROL DATA
  Futures proceeds             147,649 SEK
  Futures cost                  98,558 SEK
  Options proceeds             683,529 SEK

DIFFERENCE  (calculated − Skatteverket)
  Futures proceeds             +451 SEK  (0.3%)
  Futures cost                 -108 SEK  (0.1%)
  Options proceeds             -529 SEK  (0.1%)

Status: OK (within 5% tolerance)
```

---

## Skatteverket Reconciliation

Every year IBKR submits summary data to Skatteverket on behalf of Swedish
account holders.  These figures appear pre-filled in the customer's
*Inkomstdeklaration 1* under:

| Skatteverket label                         | Field              |
|--------------------------------------------|--------------------|
| Övriga terminer – erhållen ersättning      | `futures_proceeds` |
| Övriga terminer – erlagd ersättning        | `futures_cost`     |
| Övriga optioner                            | `options_proceeds` |

### PDF parsing (`skv_parser.py`)

The `--tax-pdf` flag triggers `skv_parser.parse_skv_pdf()`, which:

1. Extracts all text from the PDF with **pdfplumber**.
2. Searches for the Swedish key phrases (case-insensitive; handles both
   hyphen `-` and en-dash `–` variants).
3. Extracts the numeric value on the same line or up to 3 lines below.
4. Accepts space-separated (`147 649`), comma-separated (`147,649`), and
   plain (`147649`) number formats.

The resulting `SkvControlData` object feeds directly into
`compute_reconciliation()` via `.as_skv_kwargs()`.

### Tolerance

The default tolerance is **5 %**.  Override with `--reconciliation-tolerance`:

```bash
# Strict: warn if deviation exceeds 1 %
python main.py --input data/raw/activity.csv --start 2025-01-01 --end 2025-12-31 \
  --tax-pdf data/raw/deklaration.pdf \
  --reconciliation-tolerance 0.01
```

---

## Project Structure

```
ibkr-k4-tax/
├── main.py              # CLI entry point & pipeline orchestration
├── parser.py            # IBKR Activity Statement CSV parser
├── fx.py                # Historical FX rates — Riksbank SWEA API + cache
├── k4_generator.py      # SEK conversion, K4 categorisation, CSV/JSON export
├── reconciliation.py    # Section A/D totals, derivative breakdown, SKV diffs
├── skv_parser.py        # Skatteverket declaration PDF parser (pdfplumber)
├── models.py            # Pydantic data models (Trade, K4Trade, …)
├── utils.py             # Logging helpers, date parsing, numeric utilities
├── config.py            # All configuration constants
│
├── tests/
│   ├── test_parser.py         # 24 tests — IBKR CSV parsing
│   ├── test_fx.py             # 17 tests — Riksbank FX API + caching
│   ├── test_k4.py             # 29 tests — SEK conversion & K4 logic
│   ├── test_reconciliation.py # 48 tests — reconciliation module
│   └── test_skv_parser.py     # 38 tests — Skatteverket PDF parser
│
├── data/
│   ├── raw/           # Place IBKR CSV exports and Skatteverket PDFs here  (git-ignored except *.pdf)
│   └── processed/     # Generated outputs & FX cache  (git-ignored)
│
├── requirements.txt
└── README.md
```

---

## Running Tests

```bash
conda activate ibkr-k4-tax

# Run all 156 tests
pytest

# Verbose output
pytest -v

# With coverage report
pytest --cov=. --cov-report=term-missing

# Single module
pytest tests/test_skv_parser.py -v
```

---

## Configuration

All constants are centralised in `config.py`.  Key settings:

| Constant                    | Default                              | Description                                       |
|-----------------------------|--------------------------------------|---------------------------------------------------|
| `FX_API_BASE_URL`           | `https://api.riksbank.se/swea/v1`    | Sveriges Riksbank SWEA API base URL               |
| `FX_RIKSBANK_SERIES`        | See `config.py`                      | Currency → Riksbank series name mapping           |
| `FX_CACHE_FILE`             | `data/processed/fx_cache.json`       | Local FX rate cache                               |
| `FX_CACHE_TTL_DAYS`         | `30`                                 | Cache validity in days (`0` = always re-fetch)    |
| `FX_FALLBACK_RATE`          | `None`                               | Rate when API unreachable (`None` = raise error)  |
| `TARGET_CURRENCY`           | `SEK`                                | Reporting currency                                |
| `K4_SECTION_MAP`            | See `config.py`                      | Asset class → K4 section (`A` / `D`)              |
| `FUTURES_ASSET_CLASSES`     | `{"FUTURES", "FUT", "FOP", …}`       | Asset classes counted as *terminer*               |
| `OPTIONS_ASSET_CLASSES`     | `{"EQUITY AND INDEX OPTIONS", "OPT"}`| Asset classes counted as *optioner*               |
| `RECONCILIATION_TOLERANCE`  | `0.05`                               | Default max relative deviation (5 %)              |
| `SKV_PHRASE_MAP`            | See `config.py`                      | Swedish key phrase → `SkvControlData` field name  |

---

## License

MIT
