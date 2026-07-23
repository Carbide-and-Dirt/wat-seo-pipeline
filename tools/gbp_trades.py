"""
gbp_trades.py — map a GBP primary category to a trade group for the excavation review scan.

Shared by gbp_review_batches.py (which businesses to scan) and gbp_pitch_list.py (how to tab
them). Project rule (2026-07-05): the excavation pitch keeps excavation + construction + septic
+ plumbers; it drops the other trades (HVAC, restoration, roofing, electrical, landscaping, etc.).
Groups, closest-fit first: excavation, septic, plumber. Everything else -> 'other' (excluded).
"""

# True dirt-work / site-work categories (project rule: this group is "excavation").
EXCAVATION_CORE = {
    "Excavating contractor",
    "Demolition contractor",
    "Concrete contractor",
    "Drainage service",
    "Utility contractor",
    "Earth works company",
    "Well drilling contractor",
    "Foundation",
    "Retaining wall supplier",
    "Dock builder",
    "Sand & gravel supplier",
    "Asphalt contractor",
    "Grading contractor",
    "Land clearing service",
    "Forestry service",
    "Scrap metal dealer",
    "Waste management service",
    "Junk removal service",
    "Dumpster rental service",
    "Paving contractor",
}
SEPTIC = {"Septic system service"}
PLUMBER = {"Plumber"}
# Generic construction (mostly general-build / remodel, NOT dirt work). Its own tab, take-or-leave
# (project rule, 2026-07-05) — kept out of "excavation" so that list stays pure dirt-work.
CONSTRUCTION = {"Construction company", "General contractor", "Contractor"}

# Display order (closest fit to the excavation vertical first).
GROUP_ORDER = ["excavation", "septic", "plumber", "construction"]


def trade_group(category):
    """excavation | septic | plumber | construction | other. 'other' = out-of-scope trade."""
    if category in EXCAVATION_CORE:
        return "excavation"
    if category in SEPTIC:
        return "septic"
    if category in PLUMBER:
        return "plumber"
    if category in CONSTRUCTION:
        return "construction"
    return "other"


def is_included(category):
    """True if the category is in-scope for the excavation review scan (not an other-trade)."""
    return trade_group(category) != "other"
