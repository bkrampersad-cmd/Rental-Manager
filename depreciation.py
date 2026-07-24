"""Residential rental property depreciation (IRS Schedule E, line 18).

The IRS requires residential rental real property placed in service after
1986 to be depreciated using MACRS straight-line over 27.5 years under the
mid-month convention (IRC Sec. 168; see IRS Pub. 946). This isn't optional
or an "election" like it is for personal property — real estate only ever
uses this one method, so unlike the general-purpose MACRS calculator this
was ported from (which exposes DB/SL, Half-Year/Mid-Quarter/Mid-Month, and
various property-class lives for personal property), this module hard-codes
the one combination that actually applies here: method="SL", factor=1,
convention="Mid-Month", life=27.5 years.

The core math (mid-month first-year fraction, last-year calculation, and
the year-by-year schedule walk) is ported as-is from the "MACRS Full"
calculator in CalculatorSuite (calculator_suite.py), which was built and
verified against real IRS depreciation tables previously — reused here
rather than re-derived, so the underlying formulas are already proven.
"""
import math
from datetime import date


RESIDENTIAL_RENTAL_LIFE_YEARS = 27.5


def _mid_month_first_year_fraction(placed_month):
    """Fraction of a full year's depreciation allowed in year 1, given the
    property was placed in service in calendar month `placed_month` (1-12).
    Mid-month convention treats the property as placed in service at the
    midpoint of that month."""
    m = int(placed_month)
    return (12.5 - m) / 12


def _mid_month_last_year(n, placed_month):
    """Which tax year (1-indexed from the placed-in-service year) the final,
    partial-year depreciation deduction falls in."""
    m = int(placed_month)
    return math.ceil(n + (m - 0.5) / 12)


def residential_rental_schedule(depreciable_basis, placed_in_service_date):
    """Full year-by-year straight-line depreciation schedule for a
    residential rental property, mid-month convention, 27.5-year life.

    `depreciable_basis` is the building's cost basis (purchase price minus
    land value — land is never depreciable). `placed_in_service_date` is a
    `date` (the day the property was ready and available for rent, not
    necessarily the purchase date if renovated before its first tenant).

    Returns a list of dicts, one per tax year, each with:
      calendar_year, year_index (1 = the placed-in-service year),
      deduction, cumulative, book_value.
    Returns [] if the basis is zero/negative or no placed-in-service date.
    """
    if not depreciable_basis or depreciable_basis <= 0 or not placed_in_service_date:
        return []

    n = RESIDENTIAL_RENTAL_LIFE_YEARS
    placed_month = placed_in_service_date.month
    placed_year = placed_in_service_date.year
    f1 = _mid_month_first_year_fraction(placed_month)
    last = _mid_month_last_year(n, placed_month)
    annual_sl = depreciable_basis / n

    rows = []
    remaining = depreciable_basis
    cumulative = 0.0

    for yr in range(1, last + 1):
        if remaining <= 1e-6:
            break
        if yr == 1:
            d = annual_sl * f1
        elif yr == last:
            d = remaining
        else:
            d = annual_sl
        d = min(max(d, 0), remaining)
        cumulative += d
        remaining = depreciable_basis - cumulative
        rows.append({
            "calendar_year": placed_year + yr - 1,
            "year_index": yr,
            "deduction": round(d, 2),
            "cumulative": round(cumulative, 2),
            "book_value": round(max(remaining, 0), 2),
        })

    return rows


def depreciation_for_tax_year(depreciable_basis, placed_in_service_date, tax_year):
    """The single tax year's depreciation deduction (Schedule E line 18) —
    0 if the property wasn't yet in service that year, or is fully
    depreciated, or is missing the inputs needed to compute it at all."""
    for row in residential_rental_schedule(depreciable_basis, placed_in_service_date):
        if row["calendar_year"] == tax_year:
            return row["deduction"]
    return 0.0
