"""
models.py
---------
Pydantic data models for IBKR trade records and K4 tax output.
"""

from datetime import datetime
from typing import Literal, Optional

from pydantic import BaseModel, Field, field_validator


K4Section = Literal["A", "D"]


class Trade(BaseModel):
    """
    Normalised representation of a single IBKR trade row extracted
    from the *Trades* section of an Activity Statement CSV.
    """

    date: datetime
    symbol: str
    asset_class: str  # STK, ETF, OPT, FOP, FUT, CFD, …
    quantity: float
    price: float
    proceeds: float
    fees: float
    currency: str
    realized_pnl: float

    @field_validator("symbol", "asset_class", "currency", mode="before")
    @classmethod
    def strip_whitespace(cls, v: str) -> str:
        return v.strip()

    @field_validator("quantity", "price", "proceeds", "fees", "realized_pnl", mode="before")
    @classmethod
    def coerce_numeric(cls, v) -> float:
        """Accept numeric strings with optional commas."""
        if isinstance(v, str):
            v = v.replace(",", "").strip()
        return float(v)


class K4Trade(BaseModel):
    """
    A single trade enriched with SEK-converted amounts and K4 section.
    Ready for export to *trades_sek.csv*.
    """

    date: datetime
    symbol: str
    asset_class: str
    quantity: float
    sale_amount_sek: float = Field(..., description="Proceeds converted to SEK")
    purchase_amount_sek: float = Field(..., description="Cost basis converted to SEK")
    profit_loss_sek: float = Field(..., description="Net profit/loss in SEK")
    k4_section: K4Section

    @property
    def is_profit(self) -> bool:
        return self.profit_loss_sek >= 0


class K4SectionSummary(BaseModel):
    """
    Aggregated totals for one K4 section (A or D).
    Used in *k4_summary.json*.
    """

    total_sales: float = 0.0
    total_cost: float = 0.0
    profit: float = 0.0
    loss: float = 0.0


class K4SymbolSummary(BaseModel):
    """
    Per-symbol aggregated totals.
    One row in *k4_summary.csv*.
    """

    symbol: str
    k4_section: K4Section
    total_sales_sek: float = 0.0
    total_cost_sek: float = 0.0
    profit_sek: float = 0.0
    loss_sek: float = 0.0
