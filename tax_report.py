"""Builds the Tax Documents report: a Schedule E-style income/expense
breakdown per property for a given calendar tax year, ready to hand to a
CPA. Used both for the on-screen preview (JSON) and to feed the PDF builder
in exports.py — this module only computes the numbers, it doesn't render
anything.
"""
from datetime import date

from models import (
    db, Transaction, Category, MileageLog,
    SCHEDULE_E_LINES, SCHEDULE_E_NOT_DEDUCTIBLE, SCHEDULE_E_CAPITALIZE,
)
import depreciation as dep_lib

SCHEDULE_E_LINE_ORDER = [key for key, _, _ in SCHEDULE_E_LINES]
SCHEDULE_E_LINE_LABELS = {key: label for key, _, label in SCHEDULE_E_LINES}
SCHEDULE_E_LINE_NUMBERS = {key: num for key, num, _ in SCHEDULE_E_LINES}


def _parse_date(s):
    if not s:
        return None
    try:
        return date.fromisoformat(s[:10])
    except (ValueError, TypeError):
        return None


def build_property_tax_report(prop, tax_year, include_detail=False):
    """Computes one property's Schedule E-style figures for `tax_year`
    (a calendar year — Jan 1 through Dec 31, matching how Schedule E works
    for individuals regardless of the app's internal fiscal-year setting).

    Returns a dict with: property, rents_received, lines (ordered list of
    {key, number, label, amount, transactions? }), depreciation,
    not_deductible, capital_improvements, unmapped (list of
    {category_name, amount, transactions?}), total_expenses (includes
    depreciation), net.
    """
    start = f"{tax_year}-01-01"
    end = f"{tax_year}-12-31"

    txns = (
        Transaction.query
        .filter(Transaction.property_id == prop.id)
        .filter(Transaction.date >= start, Transaction.date <= end)
        .order_by(Transaction.date)
        .all()
    )

    rents_received = 0.0
    rents_txns = []
    line_totals = {key: 0.0 for key in SCHEDULE_E_LINE_ORDER}
    line_txns = {key: [] for key in SCHEDULE_E_LINE_ORDER}
    not_deductible = 0.0
    not_deductible_txns = []
    capital_improvements = 0.0
    capital_improvement_txns = []
    unmapped = {}   # category_name -> {"amount": x, "transactions": [...]}
    adjustments = 0.0   # net effect of reconciliation adjustments, informational only
    adjustment_txns = []

    for t in txns:
        # Reconciliation adjustments correct the app's books to match a bank
        # statement — they aren't real rental income or a real deductible
        # expense, so they're kept out of the Schedule E totals entirely and
        # surfaced separately instead (see build_property_tax_report's
        # "adjustments" key).
        if t.is_adjustment:
            adjustments += t.amount if t.type == "income" else -t.amount
            adjustment_txns.append(t)
            continue

        if t.type == "income":
            rents_received += t.amount
            rents_txns.append(t)
            continue

        line = t.category.schedule_e_line if t.category else None
        if line == SCHEDULE_E_NOT_DEDUCTIBLE:
            not_deductible += t.amount
            not_deductible_txns.append(t)
        elif line == SCHEDULE_E_CAPITALIZE:
            capital_improvements += t.amount
            capital_improvement_txns.append(t)
        elif line in line_totals:
            line_totals[line] += t.amount
            line_txns[line].append(t)
        else:
            name = t.category.name if t.category else "Uncategorized"
            bucket = unmapped.setdefault(name, {"amount": 0.0, "transactions": []})
            bucket["amount"] += t.amount
            bucket["transactions"].append(t)

    depreciable_basis = None
    depreciation_amount = 0.0
    if prop.purchase_price:
        depreciable_basis = max((prop.purchase_price or 0) - (prop.land_value or 0), 0)
    placed = _parse_date(prop.placed_in_service_date) or _parse_date(prop.purchase_date)
    if depreciable_basis and placed:
        depreciation_amount = dep_lib.depreciation_for_tax_year(depreciable_basis, placed, tax_year)

    # Standard-mileage-rate trips logged against this property fold into the
    # Auto and Travel line alongside any transactions already tagged there
    # — the two are complementary ways of capturing the same deduction (a
    # paid invoice vs. your own vehicle use), not competing ones.
    mileage_logs = (
        MileageLog.query
        .filter(MileageLog.property_id == prop.id)
        .filter(MileageLog.date >= start, MileageLog.date <= end)
        .all()
    )
    mileage_miles = sum(m.miles for m in mileage_logs)
    mileage_deduction = sum(m.miles * m.rate_used for m in mileage_logs)
    # Trips logged via a Transaction's "Distance" option already have their
    # dollar amount flowing into this line through the transaction-category
    # rollup above (line_totals is built from Transaction rows earlier in
    # this function) — only fold the *unlinked* trips' deduction in here, or
    # those dollars would be counted twice. The "mileage" summary block below
    # still reports on every trip, linked or not, so the property's total
    # logged mileage for the year is always visible in one place.
    unlinked_deduction = sum(m.miles * m.rate_used for m in mileage_logs if m.transaction_id is None)
    if unlinked_deduction:
        line_totals["auto_travel"] += unlinked_deduction

    lines = []
    for key in SCHEDULE_E_LINE_ORDER:
        entry = {
            "key": key,
            "number": SCHEDULE_E_LINE_NUMBERS[key],
            "label": SCHEDULE_E_LINE_LABELS[key],
            "amount": round(line_totals[key], 2),
        }
        if include_detail:
            entry["transactions"] = [tx.to_dict() for tx in line_txns[key]]
        lines.append(entry)

    total_expenses = sum(line_totals.values()) + depreciation_amount

    report = {
        "property_id": prop.id,
        "property_name": prop.name,
        "property_address": prop.address,
        "property_state": prop.state,
        "tax_year": tax_year,
        "rents_received": round(rents_received, 2),
        "lines": lines,
        "depreciation": {
            "amount": round(depreciation_amount, 2),
            "depreciable_basis": round(depreciable_basis, 2) if depreciable_basis else None,
            "placed_in_service_date": placed.isoformat() if placed else None,
            "missing_inputs": not (depreciable_basis and placed),
        },
        "mileage": {
            "miles": round(mileage_miles, 1),
            "deduction": round(mileage_deduction, 2),
            "trip_count": len(mileage_logs),
        },
        "not_deductible": {
            "amount": round(not_deductible, 2),
            "label": "Mortgage Principal (not deductible — tracked for reference only)",
        },
        "capital_improvements": {
            "amount": round(capital_improvements, 2),
            "label": "Capital Improvements (must be capitalized/depreciated, not expensed)",
        },
        "adjustments": {
            "amount": round(adjustments, 2),
            "label": "Reconciliation Adjustments (bookkeeping corrections, not real income/expense — "
                     "excluded from the totals above; review before filing)",
        },
        "unmapped": [
            {"category_name": name, "amount": round(v["amount"], 2),
             **({"transactions": [tx.to_dict() for tx in v["transactions"]]} if include_detail else {})}
            for name, v in sorted(unmapped.items())
        ],
        "total_expenses": round(total_expenses, 2),
        "net": round(rents_received - total_expenses, 2),
    }
    if include_detail:
        report["rents_transactions"] = [tx.to_dict() for tx in rents_txns]
        report["not_deductible_transactions"] = [tx.to_dict() for tx in not_deductible_txns]
        report["capital_improvement_transactions"] = [tx.to_dict() for tx in capital_improvement_txns]
        report["adjustment_transactions"] = [tx.to_dict() for tx in adjustment_txns]
    return report


def build_tax_packet(properties, tax_year, include_detail=False):
    """Cover-page totals + one report per property."""
    property_reports = [build_property_tax_report(p, tax_year, include_detail) for p in properties]
    return {
        "tax_year": tax_year,
        "properties": property_reports,
        "totals": {
            "rents_received": round(sum(r["rents_received"] for r in property_reports), 2),
            "total_expenses": round(sum(r["total_expenses"] for r in property_reports), 2),
            "depreciation": round(sum(r["depreciation"]["amount"] for r in property_reports), 2),
            "mileage_deduction": round(sum(r["mileage"]["deduction"] for r in property_reports), 2),
            "net": round(sum(r["net"] for r in property_reports), 2),
        },
    }


def build_vendor_report(properties, tax_year, threshold):
    """Totals every expense payee across the given properties for a
    calendar tax year, for spotting who may need a 1099-NEC/MISC.
    Informational only — this never files or generates a 1099 itself, it
    just flags payees at or above `threshold` so you know who to look at.
    Reconciliation adjustments are excluded (see build_property_tax_report)
    since they're bookkeeping corrections, not real payments to anyone."""
    start = f"{tax_year}-01-01"
    end = f"{tax_year}-12-31"
    property_ids = [p.id for p in properties]

    txns = (
        Transaction.query
        .filter(Transaction.property_id.in_(property_ids))
        .filter(Transaction.date >= start, Transaction.date <= end)
        .filter(Transaction.type == "expense")
        .filter(Transaction.is_adjustment.isnot(True))
        .all()
    )

    by_payee = {}
    for t in txns:
        name = (t.payee or "").strip() or "(no payee listed)"
        bucket = by_payee.setdefault(name, {"payee": name, "amount": 0.0, "transaction_count": 0})
        bucket["amount"] += t.amount
        bucket["transaction_count"] += 1

    vendors = [
        {**v, "amount": round(v["amount"], 2), "meets_threshold": v["amount"] >= threshold}
        for v in by_payee.values()
    ]
    vendors.sort(key=lambda v: -v["amount"])

    return {
        "tax_year": tax_year,
        "threshold": threshold,
        "vendors": vendors,
        "flagged_count": sum(1 for v in vendors if v["meets_threshold"]),
    }
