"""
assignment_handler.py
---------------------
Detect and annotate IBKR option lifecycle events (expiry, assignment,
exercise) in a list of :class:`~models.Trade` objects.

Background
~~~~~~~~~~
Interactive Brokers encodes special trade events in the ``Code`` column of
the Trades section.  Multiple codes are joined with semicolons, e.g. ``A;C``
or ``C;Ep``.  The codes relevant to option lifecycle events are:

    O   – Opening trade
    C   – Closing trade
    A   – Assignment or exercise trigger
    Ep  – Expired (position closed by expiry)

Combinations seen in real Activity Statements
---------------------------------------------
C;Ep  (``{"C", "Ep"}``)
    Option closed by expiry.  IBKR puts the full P&L in the
    ``Realized P/L`` column (proceeds = 0).  The existing pipeline
    already handles this correctly — no fix needed.

A;C   (``{"A", "C"}``)
    The option *or* futures side consumed by an assignment/exercise.
    For equity-option legs: proceeds = 0, pnl = 0 (no standalone P&L).
    For options-on-futures legs: proceeds = notional, pnl contains the
    gain/loss on the futures position that was opened and then assigned.

A;O   (``{"A", "O"}``)
    The *underlying security opened* via assignment (e.g. 100 shares of
    QQQ purchased when a short put is assigned).  IBKR encodes the debit
    as **negative** proceeds (e.g. ``proceeds = -51000``), which the
    standard pipeline misinterprets as a sale with negative revenue.
    This handler detects A;O rows and marks them as
    ``assignment_type = "AssignmentOpen"`` so that
    :func:`~k4_generator.convert_trade_to_sek` can apply the correct
    treatment: ``purchase_amount = abs(proceeds)``, ``sale_amount = 0``.

Assignment pairing
------------------
Equity option assigned:

    ┌─────────────────────────────────────┬────────────────────────────────┐
    │ Row                                 │ Code(s)                        │
    ├─────────────────────────────────────┼────────────────────────────────┤
    │ Option (e.g. QQQ 510 P)             │ A;C  → assignment_type=Assigned│
    │ Underlying stock (QQQ)              │ A;O  → assignment_type=        │
    │                                     │        AssignmentOpen          │
    └─────────────────────────────────────┴────────────────────────────────┘

Options-on-futures assigned:

    ┌─────────────────────────────────────┬────────────────────────────────┐
    │ Row                                 │ Code(s)                        │
    ├─────────────────────────────────────┼────────────────────────────────┤
    │ Option (e.g. MES 5945 C)            │ A;C  → assignment_type=Assigned│
    │ Futures contract (MESM5)            │ A;C  → assignment_type=Assigned│
    └─────────────────────────────────────┴────────────────────────────────┘

Both patterns result in ``assigned_type = "Assigned"`` for all A;C rows
(their P&L is either already zero or correctly captured in realized_pnl).
"""

import logging
from typing import Optional

from models import AssignmentType, Trade

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# IBKR code token constants
# ---------------------------------------------------------------------------
_CODE_OPEN        = "O"
_CODE_CLOSE       = "C"
_CODE_ASSIGNMENT  = "A"
_CODE_EXPIRY      = "Ep"


# ---------------------------------------------------------------------------
# Public helpers
# ---------------------------------------------------------------------------

def parse_ibkr_codes(code_str: str) -> frozenset:
    """
    Split a raw IBKR ``Code`` column string into a frozenset of tokens.

    Parameters
    ----------
    code_str:
        Raw string from the ``Code`` column, e.g. ``"A;C;P"``.
        Empty string or whitespace returns an empty frozenset.

    Returns
    -------
    frozenset[str]
        Individual code tokens (whitespace-stripped).

    Examples
    --------
    >>> parse_ibkr_codes("A;C")
    frozenset({'A', 'C'})
    >>> parse_ibkr_codes("")
    frozenset()
    >>> parse_ibkr_codes("C;Ep")
    frozenset({'C', 'Ep'})
    """
    return frozenset(token.strip() for token in code_str.split(";") if token.strip())


def classify_assignment_type(codes: frozenset) -> Optional[AssignmentType]:
    """
    Return the :data:`~models.AssignmentType` for a set of IBKR code tokens.

    Decision rules (applied in priority order):

    1. ``"A" in codes and "O" in codes`` → ``"AssignmentOpen"``
       (underlying security *opened* via assignment; proceeds are a debit)
    2. ``"A" in codes and "C" in codes`` → ``"Assigned"``
       (option/futures side *consumed* by assignment/exercise)
    3. ``"Ep" in codes``                 → ``"Expired"``
       (position closed by expiry; P&L already in realized_pnl)
    4. Otherwise                          → ``None``
       (ordinary opening or closing trade)

    Parameters
    ----------
    codes:
        frozenset of individual IBKR code tokens for one trade row.

    Returns
    -------
    :data:`~models.AssignmentType` or ``None``.
    """
    if _CODE_ASSIGNMENT in codes and _CODE_OPEN in codes:
        return "AssignmentOpen"
    if _CODE_ASSIGNMENT in codes and _CODE_CLOSE in codes:
        return "Assigned"
    if _CODE_EXPIRY in codes:
        return "Expired"
    return None


# ---------------------------------------------------------------------------
# Main pipeline step
# ---------------------------------------------------------------------------

def process_assignments(trades: list[Trade]) -> list[Trade]:
    """
    Annotate trades with their :data:`~models.AssignmentType` lifecycle role.

    This function is a **non-destructive post-processing step**: it iterates
    over every trade, determines whether it represents an option lifecycle
    event, and returns a new list with ``assignment_type`` set accordingly.
    No trades are removed — the :class:`~k4_generator.K4Generator` (via
    :func:`~k4_generator.convert_trade_to_sek`) uses ``assignment_type`` to
    choose the correct SEK-conversion logic.

    Key fixes applied
    -----------------
    * ``A;O`` rows (e.g. QQQ stock purchased via short-put assignment):
      marked ``assignment_type = "AssignmentOpen"``.  The K4 generator will
      then compute ``purchase_amount_sek = abs(proceeds) * rate`` and set
      ``sale_amount_sek = 0``, avoiding the negative-proceeds bug.

    * ``A;C`` rows (option/futures side consumed):
      marked ``assignment_type = "Assigned"``.  These already have
      ``proceeds = 0, realized_pnl = 0`` for equity-option legs, so no
      additional numeric fix is needed; the annotation improves auditability.

    * ``C;Ep`` rows (expired options):
      marked ``assignment_type = "Expired"``.  The existing P&L formula
      already produces the correct SEK amounts for these rows.

    Parameters
    ----------
    trades:
        List of :class:`~models.Trade` objects, typically from
        :func:`~parser.load_trades`.

    Returns
    -------
    list[Trade]
        New list with ``assignment_type`` populated where applicable.
        All other fields are unchanged.
    """
    annotated: list[Trade] = []
    n_expired = n_assigned = n_assignment_open = 0

    for trade in trades:
        atype = classify_assignment_type(trade.ibkr_codes)
        if atype is not None:
            trade = trade.model_copy(update={"assignment_type": atype})
            if atype == "Expired":
                n_expired += 1
            elif atype == "Assigned":
                n_assigned += 1
            elif atype == "AssignmentOpen":
                n_assignment_open += 1
        annotated.append(trade)

    if n_expired or n_assigned or n_assignment_open:
        logger.info(
            "Assignment annotation: %d expired, %d assigned/exercised, "
            "%d assignment-open rows.",
            n_expired,
            n_assigned,
            n_assignment_open,
        )

    return annotated
