"""
margin_calculator.py — Compute dollar margin impact for RAISE and LOWER scenarios.

For RAISE:
  new_unit_profit = new_price - COGS - FBA_fee - (new_price * referral_rate)
  est_units = avg_weekly_units * (1 + elasticity * raise_pct)
  weekly_impact = (new_unit_profit * est_units) - (old_unit_profit * avg_weekly_units)

For LOWER (per scenario, using viability from recommendation.py):
  Scenario A: same volume  →  (new_unit_profit - old_unit_profit) * avg_weekly_units
  Scenario B: volume increase per elasticity
  Break-even: guarded against new_unit_profit <= 0

All scenarios in lower_pcts are computed; non-viable ones are set to None and labeled blocked.
"""

import logging
from typing import Any, Optional

import numpy as np
import pandas as pd

logger = logging.getLogger(__name__)


def _unit_profit(price: float, cogs: float, fba_fee: float, referral_rate: float,
                 tacos_rate: float = 0.0) -> float:
    # Bug 4 fix: include TACoS in unit profit so scenario dollar impacts use
    # true contribution margin, not structural margin.
    return price - cogs - fba_fee - (price * referral_rate) - (price * tacos_rate)


def calculate_margin_impact(rec_row: pd.Series, config: dict) -> dict:
    """Compute all raise/lower scenario impacts for one recommendation row.

    Returns a flat dict of impact columns to merge back into recommendations_df.
    """
    effective_brand = rec_row.get("effective_brand") or rec_row.get("brand", "nl_brands_other")
    brand_config = config["brands"].get(effective_brand,
                   config["brands"].get("nl_brands_other", {}))
    brand_elasticity = brand_config.get(
        "price_elasticity",
        config["price_scenarios"].get("default_elasticity", -0.8),
    )

    current_price: Optional[float] = rec_row.get("current_price")
    cogs: Optional[float] = rec_row.get("cogs")
    avg_fba_fee: float = rec_row.get("avg_fba_fee_per_unit") or 0.0
    avg_ref_rate: float = rec_row.get("avg_referral_rate") or 0.15
    _tacos_raw = rec_row.get("avg_tacos_rate")
    avg_tacos_rate: float = (
        0.0 if (_tacos_raw is None or (isinstance(_tacos_raw, float) and np.isnan(_tacos_raw)))
        else float(_tacos_raw)
    )  # Bug A fix: np.nan is truthy so "or 0.0" didn't catch it — explicit isnan guard required
    avg_weekly_units: Optional[float] = rec_row.get("avg_weekly_units")
    scenario_viable: dict = rec_row.get("scenario_viable") or {}

    result: dict[str, Any] = {}

    # If we can't compute anything meaningful, return empty impact
    if not current_price or not avg_weekly_units or current_price <= 0 or avg_weekly_units <= 0:
        logger.debug("Insufficient data for margin impact on ASIN %s", rec_row.get("asin"))
        return result

    if cogs is None or (isinstance(cogs, float) and np.isnan(cogs)):
        return result  # No COGS → no margin impact (HOLD path)

    old_unit_profit = _unit_profit(current_price, cogs, avg_fba_fee, avg_ref_rate, avg_tacos_rate)

    # ── RAISE scenarios ────────────────────────────────────────────────────
    for raise_pct in config["price_scenarios"]["raise_pcts"]:
        new_price = current_price * (1 + raise_pct)
        new_unit_profit = _unit_profit(new_price, cogs, avg_fba_fee, avg_ref_rate, avg_tacos_rate)
        # Elasticity: higher price → fewer units (elasticity is negative)
        est_units = avg_weekly_units * (1 + brand_elasticity * raise_pct)
        est_units = max(0.0, est_units)
        weekly_impact = (new_unit_profit * est_units) - (old_unit_profit * avg_weekly_units)
        pct_label = int(raise_pct * 100)
        result[f"raise_{pct_label}pct_weekly_impact"] = round(weekly_impact, 2)
        result[f"raise_{pct_label}pct_new_price"] = round(new_price, 2)

    # ── LOWER scenarios ────────────────────────────────────────────────────
    for lower_pct in config["price_scenarios"]["lower_pcts"]:
        pct_label = int(lower_pct * 100)
        viable = scenario_viable.get(lower_pct, False)

        if not viable:
            result[f"lower_{pct_label}pct_scenario_a"] = None
            result[f"lower_{pct_label}pct_scenario_b"] = None
            result[f"lower_{pct_label}pct_break_even_pct"] = None
            result[f"lower_{pct_label}pct_viable"] = False
            result[f"lower_{pct_label}pct_new_price"] = None
            continue

        new_price = current_price * (1 - lower_pct)
        new_unit_profit = _unit_profit(new_price, cogs, avg_fba_fee, avg_ref_rate, avg_tacos_rate)
        result[f"lower_{pct_label}pct_new_price"] = round(new_price, 2)
        result[f"lower_{pct_label}pct_viable"] = True

        # Scenario A: same volume
        scenario_a = (new_unit_profit - old_unit_profit) * avg_weekly_units
        result[f"lower_{pct_label}pct_scenario_a"] = round(scenario_a, 2)

        # Scenario B: volume increase per elasticity (lower price → positive volume effect)
        # elasticity is negative, lower_pct is positive → -(lower_pct) makes price drop
        # volume_change = elasticity * (-lower_pct) → positive (more units)
        vol_change_factor = 1 + brand_elasticity * (-lower_pct)
        vol_change_factor = max(0.0, vol_change_factor)
        est_units_b = avg_weekly_units * vol_change_factor
        scenario_b = (new_unit_profit * est_units_b) - (old_unit_profit * avg_weekly_units)
        result[f"lower_{pct_label}pct_scenario_b"] = round(scenario_b, 2)

        # Break-even: volume increase needed to match current profit
        # Guard: if new_unit_profit <= 0, break-even is undefined
        if new_unit_profit <= 0:
            result[f"lower_{pct_label}pct_break_even_pct"] = None
        else:
            # break_even_units = (old_unit_profit * avg_weekly_units) / new_unit_profit
            # break_even_pct = (break_even_units / avg_weekly_units - 1) * 100
            break_even_pct = (old_unit_profit / new_unit_profit - 1) * 100
            result[f"lower_{pct_label}pct_break_even_pct"] = round(break_even_pct, 1)

    return result


def calculate_all_margin_impacts(recommendations_df: pd.DataFrame, config: dict) -> pd.DataFrame:
    """Apply calculate_margin_impact to every row in recommendations_df.

    Returns recommendations_df with additional margin impact columns merged in.
    """
    if recommendations_df.empty:
        return recommendations_df

    impact_records = []
    for _, row in recommendations_df.iterrows():
        impact = calculate_margin_impact(row, config)
        impact_records.append(impact)

    if not impact_records:
        return recommendations_df

    impact_df = pd.DataFrame(impact_records, index=recommendations_df.index)
    # Merge impact columns into recommendations_df
    result = pd.concat([recommendations_df, impact_df], axis=1)
    return result
