"""
recommendation.py — Generate RAISE/LOWER/HOLD recommendations and run look-back analysis.

Recommendation logic:
  - RAISE: composite_score > raise_score threshold
  - LOWER: composite_score < lower_score threshold AND 5% price cut keeps margin above floor
  - LOWER_BLOCKED: would recommend LOWER but margin floor prevents even the 5% scenario
  - HOLD: within normal range, or no COGS data

Per-scenario viability:
  - 5% viability check determines LOWER vs LOWER_BLOCKED
  - 10% scenario checked independently (can be blocked even when 5% is viable)
  - scenario_viable dict stored for margin_calculator

Outcome values (look-back): ONLY "success" | "underperforming" | "pending"
"""

import logging
from typing import Optional

import numpy as np
import pandas as pd

logger = logging.getLogger(__name__)

VALID_OUTCOMES = {"success", "underperforming", "pending"}


# ---------------------------------------------------------------------------
# Reasoning string builder
# ---------------------------------------------------------------------------

def _build_reasoning(row: pd.Series, rec: str, confidence: str) -> str:
    parts = []

    if pd.notna(row.get("avg_ctr")) and pd.notna(row.get("brand_avg_ctr")) and row["brand_avg_ctr"]:
        pct_vs_avg = (row["avg_ctr"] / row["brand_avg_ctr"] - 1) * 100
        if row["ctr_score"] != 0:
            parts.append(f"CTR {pct_vs_avg:+.0f}% vs brand avg")

    if pd.notna(row.get("avg_conversion")) and pd.notna(row.get("baseline_conversion")) and row["baseline_conversion"]:
        cr_change = (row["avg_conversion"] / row["baseline_conversion"] - 1) * 100
        if row["conversion_score"] != 0:
            parts.append(f"CR {cr_change:+.0f}% vs 90d baseline")

    if pd.notna(row.get("avg_units_daily")) and pd.notna(row.get("prior_units_daily")) and row["prior_units_daily"]:
        vel_change = (row["avg_units_daily"] / row["prior_units_daily"] - 1) * 100
        if row["velocity_score"] != 0:
            # Cap display at +/-500% — values beyond this are noise from near-zero prior periods
            # (the signal itself is already guarded in _score_velocity; this is display only)
            display_change = max(-500.0, min(500.0, vel_change))
            suffix = " (thin prior)" if abs(vel_change) > 500 else ""
            parts.append(f"velocity {display_change:+.0f}%{suffix} period-over-period")

    if pd.notna(row.get("current_margin")) and pd.notna(row.get("margin_floor")):
        if row["margin_score"] != 0:
            parts.append(f"margin {row['current_margin']:.0%} (floor {row['margin_floor']:.0%})")

    if row.get("trend_score", 0) != 0:
        direction = "up" if row["trend_score"] > 0 else "down"
        parts.append(f"profit trend {direction}")

    if row.get("refund_score", 0) != 0 and pd.notna(row.get("avg_refund_rate")):
        rate = row["avg_refund_rate"]
        rate_pct = rate if rate <= 1.0 else rate / 100.0
        # When returns are extreme AND the product is below margin floor, make clear
        # that returns are the primary cause — pricing action won't fix this
        if rate_pct >= 0.40 and row.get("margin_below_floor"):
            parts.append(
                f"⚠ returns ({rate_pct:.1%}) are the primary margin issue — "
                f"pricing action unlikely to resolve"
            )
        elif rate_pct >= 0.50 and rec == "RAISE":
            # High-return product being recommended for a raise — warn before acting
            parts.append(
                f"⚠ verify return rate ({rate_pct:.1%}) before raising price"
            )
        else:
            parts.append(f"refund rate {rate_pct:.1%} elevated")

    if row.get("inventory_score", 0) > 0 and pd.notna(row.get("days_of_supply")):
        parts.append(f"low stock ({row['days_of_supply']:.0f} days supply)")

    if row.get("seasonal") and pd.notna(row.get("yoy_factor")):
        parts.append(f"YoY velocity ratio {row['yoy_factor']:.2f}")

    reasoning_body = ", ".join(parts) if parts else "no strong signals"
    return f"{rec} ({confidence}): {reasoning_body}"


# ---------------------------------------------------------------------------
# Main recommendation generator
# ---------------------------------------------------------------------------

def generate_recommendations(signals_df: pd.DataFrame, config: dict) -> pd.DataFrame:
    """Generate RAISE/LOWER/HOLD recommendation for each ASIN row."""
    if signals_df.empty:
        logger.error("signals_df is empty — no recommendations generated")
        return pd.DataFrame()

    thresholds = config["signals"]["thresholds"]
    raise_score_threshold = thresholds["raise_score"]
    lower_score_threshold = thresholds["lower_score"]
    lower_pcts = config["price_scenarios"]["lower_pcts"]
    min_lower_pct = min(lower_pcts)

    conf_cfg = config["confidence"]
    high_min = conf_cfg["high_min_signals"]
    med_min = conf_cfg["medium_min_signals"]
    min_completeness = conf_cfg["min_data_completeness"]

    signal_score_cols = [
        "ctr_score", "conversion_score", "velocity_score",
        "margin_score", "trend_score", "refund_score", "inventory_score",
        "bsr_score",
    ]

    records = []
    for _, row in signals_df.iterrows():
        effective_brand = row.get("effective_brand") or row.get("brand", "nl_brands_other")
        brand_config = config["brands"].get(effective_brand,
                       config["brands"].get("nl_brands_other", {}))
        margin_floor = brand_config.get("margin_floor", 0.25)

        composite = row.get("composite_score", 0.0)
        completeness = row.get("data_completeness", 0.0)

        cogs = row.get("cogs")
        cogs_source = row.get("cogs_source", "missing")
        current_price = row.get("current_price")
        avg_fba_fee = row.get("avg_fba_fee_per_unit", 0.0) or 0.0
        avg_ref_rate = row.get("avg_referral_rate", 0.15) or 0.15
        # Bug T1 fix: viability check uses STRUCTURAL margin (no TACoS).
        # The margin_floor is a structural floor — ad spend is a separate P&L line.
        # avg_tacos_rate is retained for contribution_margin display only.
        avg_tacos_rate = row.get("avg_tacos_rate") or 0.0  # informational only here

        # ── Early exit: no COGS ────────────────────────────────────────────
        if cogs is None or cogs_source in ("missing", "bad_ratio") or (isinstance(cogs, float) and np.isnan(cogs)):
            rec = "HOLD"
            reasoning = "HOLD: no COGS data available — cannot evaluate margin safety"
            confidence = "Low"
            scenario_viable = {}
            records.append(_build_row(row, rec, confidence, reasoning, scenario_viable))
            continue

        # ── Confidence ─────────────────────────────────────────────────────
        scores = [row.get(col, 0.0) for col in signal_score_cols]
        direction = 1 if composite >= 0 else -1
        signals_agree = sum(1 for s in scores if s * direction > 0)

        if signals_agree >= high_min and completeness >= 0.80:
            confidence = "High"
        elif signals_agree >= med_min or completeness >= min_completeness:
            confidence = "Medium"
        else:
            confidence = "Low"

        # ── Decision ────────────────────────────────────────────────────────
        if composite > raise_score_threshold:
            rec = "RAISE"
            scenario_viable = {}

        elif composite < lower_score_threshold:
            # E2 fix: no price data → HOLD, not LOWER_BLOCKED.
            # LOWER_BLOCKED means "signals say cut but margin floor blocks it."
            # A missing price is a data gap — we can't evaluate viability at all.
            # Note: current_price may be NaN (float) not None — use not (x > 0)
            # because NaN > 0 is False, catching both None and NaN correctly.
            if not (current_price and current_price > 0):
                rec = "HOLD"
                reasoning = "HOLD: no price data available — cannot evaluate lower viability"
                scenario_viable = {}
                records.append(_build_row(row, rec, confidence, reasoning, scenario_viable))
                continue

            # Check viability for each lower scenario independently
            scenario_viable = {}
            for lower_pct in lower_pcts:
                proposed_price = current_price * (1 - lower_pct)
                # Structural floor check — TACoS not included (floor is structural)
                proposed_margin = (
                    (proposed_price - cogs - avg_fba_fee
                     - proposed_price * avg_ref_rate) / proposed_price
                ) if proposed_price > 0 else -1.0
                scenario_viable[lower_pct] = proposed_margin >= margin_floor

            # LOWER granted if smallest pct is viable
            if scenario_viable.get(min_lower_pct, False):
                rec = "LOWER"
            else:
                rec = "LOWER_BLOCKED"
        else:
            rec = "HOLD"
            scenario_viable = {}

        reasoning = _build_reasoning(row, rec, confidence)

        # Flag products that are ALREADY operating below their margin floor
        # (distinct from LOWER_BLOCKED which means a cut would push it below)
        cm = row.get("current_margin")
        margin_below_floor = (
            cm is not None and not (isinstance(cm, float) and np.isnan(cm))
            and cm < margin_floor
        )

        records.append(_build_row(row, rec, confidence, reasoning, scenario_viable,
                                  margin_below_floor=margin_below_floor))

    if not records:
        return pd.DataFrame()

    rec_df = pd.DataFrame(records)
    logger.info(
        "Recommendations: %s RAISE | %s LOWER | %s HOLD | %s LOWER_BLOCKED",
        (rec_df["recommendation"] == "RAISE").sum(),
        (rec_df["recommendation"] == "LOWER").sum(),
        (rec_df["recommendation"] == "HOLD").sum(),
        (rec_df["recommendation"] == "LOWER_BLOCKED").sum(),
    )
    return rec_df


def _build_row(row: pd.Series, rec: str, confidence: str, reasoning: str,
               scenario_viable: dict, margin_below_floor: bool = False) -> dict:
    """Build a flat recommendation record from a signal row."""
    return {
        "asin": row.get("asin"),
        "brand": row.get("brand"),
        "sub_brand": row.get("sub_brand"),
        "effective_brand": row.get("effective_brand"),
        "title": row.get("title"),
        "cogs_name": row.get("cogs_name"),   # clean product name from COGS sheet
        "product_category": row.get("product_category"),
        "product_family": row.get("product_family"),
        "anchor_date": row.get("anchor_date"),
        "current_price": row.get("current_price"),
        "price_source": row.get("price_source"),
        "cogs": row.get("cogs"),
        "cogs_source": row.get("cogs_source"),
        "current_margin": row.get("current_margin"),           # structural
        "contribution_margin": row.get("contribution_margin"), # after ads (informational)
        "margin_floor": row.get("margin_floor"),
        "avg_referral_rate": row.get("avg_referral_rate"),
        "avg_fba_fee_per_unit": row.get("avg_fba_fee_per_unit"),
        "avg_tacos_rate": row.get("avg_tacos_rate"),
        "avg_weekly_units": row.get("avg_weekly_units"),
        "days_of_supply": row.get("days_of_supply"),
        "sales_rank": row.get("sales_rank"),
        # Signal scores
        "ctr_score": row.get("ctr_score"),
        "conversion_score": row.get("conversion_score"),
        "velocity_score": row.get("velocity_score"),
        "margin_score": row.get("margin_score"),
        "trend_score": row.get("trend_score"),
        "refund_score": row.get("refund_score"),
        "inventory_score": row.get("inventory_score"),
        "bsr_score": row.get("bsr_score"),
        "yoy_factor": row.get("yoy_factor"),
        "composite_score": row.get("composite_score"),
        "data_completeness": row.get("data_completeness"),
        # Signal raw values
        "avg_ctr": row.get("avg_ctr"),
        "brand_avg_ctr": row.get("brand_avg_ctr"),
        "avg_conversion": row.get("avg_conversion"),
        "baseline_conversion": row.get("baseline_conversion"),
        "avg_units_daily": row.get("avg_units_daily"),
        "avg_refund_rate": row.get("avg_refund_rate"),
        "avg_margin_pct": row.get("avg_margin_pct"),
        "seasonal": row.get("seasonal"),
        # Recommendation output
        "recommendation": rec,
        "confidence": confidence,
        "reasoning": reasoning,
        # Per-scenario viability (passed to margin_calculator)
        "scenario_viable": scenario_viable,
        # Pre-change baselines for History tab
        "pre_conversion": row.get("pre_conversion"),
        "pre_units_weekly": row.get("pre_units_weekly"),
        "pre_net_profit_weekly": row.get("pre_net_profit_weekly"),
        "pre_margin_pct": row.get("pre_margin_pct"),
        "pre_ctr": row.get("pre_ctr"),
        "pre_refund_rate": row.get("pre_refund_rate"),
        "pre_days_of_supply": row.get("pre_days_of_supply"),
        # Alert flags
        "margin_below_floor": margin_below_floor,
    }


# ---------------------------------------------------------------------------
# Look-back analysis
# ---------------------------------------------------------------------------

def run_lookback(
    history_df: pd.DataFrame,
    current_signals_df: pd.DataFrame,
    anchor_date,
    config: dict,
) -> pd.DataFrame:
    """Update History tab rows for recommendations that were implemented.

    Only processes the most recent implemented row per brand+asin.
    Outcome values: "success" | "underperforming" | "pending" (no other values).
    """
    if history_df.empty or current_signals_df.empty:
        return history_df

    lookback_days = config.get("lookback_period_days", 14)
    anchor_ts = pd.Timestamp(anchor_date)

    # Ensure implemented_date is datetime
    history_df = history_df.copy()
    history_df["implemented_date"] = pd.to_datetime(history_df["implemented_date"], errors="coerce")

    # Filter to eligible rows: implemented and past lookback period
    eligible_mask = (
        history_df["implemented_date"].notna() &
        ((anchor_ts - history_df["implemented_date"]).dt.days >= lookback_days)
    )
    eligible = history_df[eligible_mask].copy()

    if eligible.empty:
        logger.info("No look-back eligible rows found")
        return history_df

    # Take only the most recent implemented row per brand+asin
    eligible = (
        eligible
        .sort_values("implemented_date")
        .groupby(["brand", "asin"])
        .last()
        .reset_index()
    )

    logger.info("Look-back: processing %d eligible rows", len(eligible))

    # Index current signals by (brand, asin) for quick lookup
    current_lookup = {}
    for _, sig_row in current_signals_df.iterrows():
        key = (str(sig_row.get("brand", "")), str(sig_row.get("asin", "")))
        current_lookup[key] = sig_row

    updates = {}  # key → {outcome, revert_flag, post_*}
    for _, row in eligible.iterrows():
        key = (str(row.get("brand", "")), str(row.get("asin", "")))
        current = current_lookup.get(key)
        if current is None:
            logger.debug("Look-back: no current signals for %s — skipping", key)
            continue

        post_conversion = current.get("pre_conversion")
        post_units = current.get("pre_units_weekly")
        post_profit = current.get("pre_net_profit_weekly")
        post_margin = current.get("pre_margin_pct")

        pre_conversion = row.get("pre_conversion")
        pre_profit = row.get("pre_net_profit_weekly")

        rec = str(row.get("recommendation", "")).upper()

        if rec == "RAISE":
            if (pre_conversion and post_conversion is not None
                    and pre_conversion > 0
                    and post_conversion < pre_conversion * 0.80):
                outcome, revert_flag = "underperforming", True
            else:
                outcome, revert_flag = "success", False

        elif rec == "LOWER":
            if (pre_profit is not None and post_profit is not None
                    and post_profit > pre_profit):
                outcome, revert_flag = "success", False
            else:
                outcome, revert_flag = "underperforming", True

        else:
            outcome, revert_flag = "pending", False

        # Validate outcome value
        assert outcome in VALID_OUTCOMES, f"Invalid outcome: {outcome}"

        updates[key] = {
            "outcome": outcome,
            "revert_flag": revert_flag,
            "post_conversion": post_conversion,
            "post_units_weekly": post_units,
            "post_net_profit_weekly": post_profit,
            "post_margin_pct": post_margin,
        }

    # Apply updates to history_df
    # Match on brand+asin+max(implemented_date)
    for idx, row in history_df.iterrows():
        key = (str(row.get("brand", "")), str(row.get("asin", "")))
        if key in updates:
            for col, val in updates[key].items():
                history_df.at[idx, col] = val

    logger.info("Look-back: updated %d rows", len(updates))
    return history_df
