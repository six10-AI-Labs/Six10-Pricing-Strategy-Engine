"""
signal_computer.py — Compute pricing signals per ASIN for the Pricing Strategy Engine.

Signals computed (7 + YoY factor):
  1. CTR          — vs brand average CTR
  2. Conversion   — vs ASIN's own 90-day baseline
  3. Velocity     — current vs prior 30-day units/day (YoY-adjusted for seasonal brands)
  4. Margin       — current margin vs brand margin floor
  5. Trend        — linear regression on 30-day Net profit trend
  6. Refund Rate  — elevated refund rate is a pricing risk
  7. Inventory Pressure — low days-of-supply guards against LOWER recommendation

Pre-change baselines (pre_*) are captured for History tab look-back.
COGS fallback chain: COGS Sheet → Sellerise CoG column → None (warn).
Current price resolution: Sellerise prod_sales_per_unit (median of last 7 sale days) → FBA your_price → None (warn).
"""

import logging
from typing import Optional

import numpy as np
import pandas as pd
from scipy import stats

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Date window helpers
# ---------------------------------------------------------------------------

def _window(df: pd.DataFrame, start: pd.Timestamp, end: pd.Timestamp) -> pd.DataFrame:
    return df[(df["date"] >= start) & (df["date"] <= end)]


def _safe_mean(series: pd.Series) -> Optional[float]:
    vals = series.dropna()
    return float(vals.mean()) if len(vals) > 0 else None


def _completeness(series: pd.Series, window_days: int) -> float:
    return series.dropna().count() / max(window_days, 1)


# ---------------------------------------------------------------------------
# Signal scoring functions
# ---------------------------------------------------------------------------

def _score_ctr(asin_ctr: Optional[float], brand_avg_ctr: Optional[float], thresholds: dict) -> float:
    if asin_ctr is None or brand_avg_ctr is None or brand_avg_ctr == 0:
        return 0.0
    ratio = asin_ctr / brand_avg_ctr
    if ratio > thresholds["ctr_raise_factor"]:
        return 1.0
    if ratio < thresholds["ctr_lower_factor"]:
        return -1.0
    return 0.0


def _score_conversion(current_cr: Optional[float], baseline_cr: Optional[float],
                      thresholds: dict) -> float:
    if current_cr is None or baseline_cr is None or baseline_cr == 0:
        return 0.0
    change_pct = (current_cr - baseline_cr) / baseline_cr
    if change_pct > thresholds["cr_raise_pct"]:
        return 1.0
    if change_pct < thresholds["cr_lower_pct"]:
        return -1.0
    return 0.0


def _score_velocity(current_vel: Optional[float], prior_vel: Optional[float],
                    yoy_factor: Optional[float], seasonal: bool,
                    thresholds: dict) -> float:
    if current_vel is None or prior_vel is None or prior_vel == 0:
        return 0.0

    # Guard: prior period too thin to be a reliable baseline.
    # < 0.1 units/day = fewer than ~3 units in 30 days — any ratio computed from this
    # is noise, not signal (e.g. 0 vs 2 units gives +infinity; 1 vs 0.05 gives +1955%).
    if prior_vel < 0.1:
        return 0.0

    raw_change = (current_vel - prior_vel) / prior_vel

    # For seasonal brands, adjust velocity change by YoY factor
    if seasonal and yoy_factor is not None and yoy_factor > 0:
        # If YoY ratio is low (e.g., 0.3 in off-peak), suppress negative velocity signal
        # Scale: raw_change / yoy_factor — if current matches seasonal expectation, net ~0
        adjusted_change = raw_change / yoy_factor if yoy_factor > 0.1 else 0.0
    else:
        adjusted_change = raw_change

    if adjusted_change > thresholds["velocity_raise_pct"]:
        return 1.0
    if adjusted_change < thresholds["velocity_lower_pct"]:
        return -1.0
    return 0.0


def _score_margin(current_margin: Optional[float], margin_floor: float,
                  thresholds: dict) -> float:
    if current_margin is None:
        return 0.0
    comfortable_cushion = thresholds["margin_comfortable_cushion"]
    tight_cushion = thresholds["margin_tight_cushion"]
    if current_margin > margin_floor + comfortable_cushion:
        return 1.0
    if current_margin < margin_floor + tight_cushion:
        return -1.0
    return 0.0


def _score_trend(day_indices: list, net_profits: list) -> tuple:
    """Returns (score, p_value). Score: +1 if significantly positive, -1 if negative.

    Bug 8 fix: require p < 0.05 (was 0.1) AND R² > 0.25 to confirm trend explains
    meaningful variance. At p < 0.1 with 175 ASINs ~17 false signals fire per run;
    tightening to p < 0.05 + R² guard drops this to ~5–6.
    """
    clean = [(i, p) for i, p in zip(day_indices, net_profits)
             if p is not None and not np.isnan(p)]
    if len(clean) < 5:
        return 0.0, 1.0
    x, y = zip(*clean)
    try:
        slope, _, r_value, p_value, _ = stats.linregress(x, y)
    except Exception:
        return 0.0, 1.0
    if p_value < 0.05 and (r_value ** 2) > 0.25:
        return (1.0 if slope > 0 else -1.0), p_value
    return 0.0, p_value


def _score_refund_rate(avg_refund_rate: Optional[float], threshold: float) -> float:
    if avg_refund_rate is None:
        return 0.0
    # Refund rate is stored as a fraction (e.g., 0.03 = 3%) or percentage (3.0)
    # Normalize: if > 1, assume it's already in percentage form, convert
    rate = avg_refund_rate if avg_refund_rate <= 1.0 else avg_refund_rate / 100.0
    return -1.0 if rate > threshold else 0.0


def _score_inventory_pressure(days_of_supply: Optional[float], low_dos_days: int) -> float:
    if days_of_supply is None:
        return 0.0
    return 0.5 if days_of_supply < low_dos_days else 0.0


def _compute_yoy_factor(current_df: pd.DataFrame, yoy_df: pd.DataFrame) -> Optional[float]:
    if current_df.empty or yoy_df.empty:
        return None
    cur_vel = _safe_mean(current_df["units"])
    yoy_vel = _safe_mean(yoy_df["units"])
    if cur_vel is None or yoy_vel is None or yoy_vel < 0.1:
        return None
    return cur_vel / yoy_vel


def _score_bsr(rank_trend: Optional[float]) -> float:
    """Score Helium10 keyword average rank trend.

    Helium10 'Keywords Average Rank Trend' = change in average rank number.
    Lower rank number = better organic visibility (rank 1 > rank 100).
    Negative trend (rank number decreasing) → improving → bullish (+1.0).
    Positive trend (rank number increasing) → worsening → bearish (−1.0).
    Zero / None → neutral (0.0).
    """
    if rank_trend is None:
        return 0.0
    if rank_trend < 0:
        return 1.0
    if rank_trend > 0:
        return -1.0
    return 0.0


def _score_yoy(yoy_factor: Optional[float]) -> float:
    """Score year-over-year velocity ratio for seasonal brands.

    yoy_factor > 1.1 → this season running hotter than last year → RAISE supportive (+1.0)
    yoy_factor < 0.9 → this season weaker than last year → LOWER supportive (-1.0)
    0.9–1.1 → in-line with last year → neutral (0.0)
    None → no prior-year data → neutral (0.0)
    """
    if yoy_factor is None:
        return 0.0
    if yoy_factor > 1.1:
        return 1.0
    if yoy_factor < 0.9:
        return -1.0
    return 0.0


# ---------------------------------------------------------------------------
# Main function
# ---------------------------------------------------------------------------

def compute_signals(
    sellerise_df: pd.DataFrame,
    fba_df: pd.DataFrame,
    cogs_df: pd.DataFrame,
    families_df: pd.DataFrame,
    config: dict,
    helium10_df: Optional[pd.DataFrame] = None,
    fee_preview_df: Optional[pd.DataFrame] = None,
    storage_df: Optional[pd.DataFrame] = None,
    return_df: Optional[pd.DataFrame] = None,
    tpl_df: Optional[pd.DataFrame] = None,
) -> pd.DataFrame:
    """Compute all pricing signals per ASIN. Returns signals_df.

    Optional supplementary sources (all keyed by ASIN):
      helium10_df   — keyword rank trend → bsr_score signal
      fee_preview_df — Amazon fee preview → override avg_fba_fee_per_unit
      storage_df    — monthly storage fees → reduce contribution_margin
      return_df     — return reason classification → weight refund_score
      tpl_df        — 3PL buffer stock → reduce inventory urgency
    """

    if sellerise_df.empty:
        logger.error("Sellerise data is empty — cannot compute signals")
        return pd.DataFrame()

    analysis_days = config.get("analysis_window_days", 30)
    prior_days = config.get("prior_window_days", 30)
    yoy_days = config.get("yoy_window_days", 30)
    thresholds = config["signals"]["thresholds"]
    min_completeness = config["confidence"]["min_data_completeness"]

    # Anchor date
    anchor_date = sellerise_df["date"].max()
    current_start = anchor_date - pd.Timedelta(days=analysis_days)
    prior_start = anchor_date - pd.Timedelta(days=analysis_days + prior_days)
    prior_end = anchor_date - pd.Timedelta(days=analysis_days)
    baseline_start = anchor_date - pd.Timedelta(days=90)
    baseline_end   = anchor_date - pd.Timedelta(days=analysis_days)   # Bug 2 fix: non-overlapping baseline
    yoy_start = anchor_date - pd.Timedelta(days=365 + yoy_days)
    yoy_end = anchor_date - pd.Timedelta(days=365)

    logger.info("Anchor date: %s | Current window: %s to %s", anchor_date.date(),
                current_start.date(), anchor_date.date())

    # Build COGS lookup: asin → cogs, brand_key
    cogs_lookup = {}
    if not cogs_df.empty:
        for _, row in cogs_df.iterrows():
            cogs_lookup[row["asin"]] = {
            "cogs":         row["cogs"],
            "brand_key":    row["brand_key"],
            "product_name": row.get("product_name"),
            "pack":         row.get("pack"),
        }

    # Build FBA lookup: (brand, asin) → row
    fba_lookup = {}
    if not fba_df.empty:
        for _, row in fba_df.iterrows():
            fba_lookup[(row["brand"], row["asin"])] = row

    # Build product family lookup: asin → {product_category, product_family}
    family_lookup = {}
    if not families_df.empty:
        for _, row in families_df.iterrows():
            family_lookup[row["asin"]] = {
                "product_category": row.get("product_category"),
                "product_family": row.get("product_family"),
            }

    # ── Build lookups from supplementary sources ───────────────────────────

    # Helium10: asin → keyword_avg_rank_trend (and other fields)
    helium10_lookup: dict = {}
    if helium10_df is not None and not helium10_df.empty:
        for _, h10_row in helium10_df.iterrows():
            helium10_lookup[str(h10_row["asin"])] = h10_row

    # Fee Preview: (brand, asin) → fee_preview_fba
    fee_preview_lookup: dict = {}
    if fee_preview_df is not None and not fee_preview_df.empty:
        for _, fp_row in fee_preview_df.iterrows():
            key = (str(fp_row.get("brand", "")), str(fp_row["asin"]))
            if pd.notna(fp_row.get("fee_preview_fba")):
                fee_preview_lookup[key] = float(fp_row["fee_preview_fba"])

    # Storage Fees: (brand, asin) → monthly_storage_fee_per_unit
    storage_lookup: dict = {}
    if storage_df is not None and not storage_df.empty:
        for _, s_row in storage_df.iterrows():
            key = (str(s_row.get("brand", "")), str(s_row["asin"]))
            if pd.notna(s_row.get("monthly_storage_fee_per_unit")):
                storage_lookup[key] = float(s_row["monthly_storage_fee_per_unit"])

    # Return Reasons: (brand, asin) → price_related_fraction
    return_lookup: dict = {}
    if return_df is not None and not return_df.empty:
        for _, r_row in return_df.iterrows():
            key = (str(r_row.get("brand", "")), str(r_row["asin"]))
            if pd.notna(r_row.get("price_related_fraction")):
                return_lookup[key] = float(r_row["price_related_fraction"])

    # 3PL: asin → tpl_available (units)
    tpl_lookup: dict = {}
    if tpl_df is not None and not tpl_df.empty:
        for _, t_row in tpl_df.iterrows():
            if pd.notna(t_row.get("tpl_available")):
                tpl_lookup[str(t_row["asin"])] = float(t_row["tpl_available"])

    # Compute brand-level CTR averages for CTR signal.
    # Bug 2 fix: filter to ad-active days (ctr_pct > 0) only.
    # Zero-CTR days mean no ads ran — including them dilutes the brand average
    # asymmetrically across ASINs with different ad coverage patterns.
    brand_avg_ctr = {}
    if "ctr_pct" in sellerise_df.columns:
        current_all = _window(sellerise_df, current_start, anchor_date)
        _ctr_active_all = current_all[current_all["ctr_pct"] > 0]
        brand_avg_ctr = (
            _ctr_active_all.groupby("brand")["ctr_pct"]
            .mean()
            .to_dict()
        )

    # Get all unique brand+asin combinations
    asin_groups = sellerise_df.groupby(["brand", "asin"])

    rows = []
    for (brand, asin), asin_df in asin_groups:
        asin_df = asin_df.sort_values("date")

        # Determine effective brand (sub_brand only meaningful for NL Brands)
        sub_brand_raw = asin_df["sub_brand"].iloc[-1] if "sub_brand" in asin_df.columns else None
        if brand == "nl_brands" and sub_brand_raw is not None and pd.notna(sub_brand_raw) \
                and str(sub_brand_raw) not in ("nl_brands", brand, ""):
            sub_brand = str(sub_brand_raw)
            effective_brand = sub_brand
        else:
            sub_brand = None          # blank for aquadoc, visivite, etc.
            effective_brand = brand

        brand_config = config["brands"].get(effective_brand,
                       config["brands"].get("nl_brands_other", {}))
        margin_floor = brand_config.get("margin_floor", 0.25)
        seasonal = brand_config.get("seasonal", False)

        # ── Window slices ──────────────────────────────────────────────────
        cur_df = _window(asin_df, current_start, anchor_date)
        prior_df = _window(asin_df, prior_start, prior_end)
        baseline_df = _window(asin_df, baseline_start, baseline_end)  # Bug 2: prior 60 days, non-overlapping with current 30
        yoy_df = _window(asin_df, yoy_start, yoy_end)

        if cur_df.empty:
            logger.debug("No current-window data for %s / %s — skipping", brand, asin)
            continue

        # ── Data completeness per signal column ────────────────────────────
        ctr_comp = _completeness(cur_df.get("ctr_pct", pd.Series(dtype=float)), analysis_days)
        cr_comp = _completeness(cur_df.get("conversion_pct", pd.Series(dtype=float)), analysis_days)
        unit_comp = _completeness(cur_df.get("units", pd.Series(dtype=float)), analysis_days)
        profit_comp = _completeness(cur_df.get("net_profit", pd.Series(dtype=float)), analysis_days)
        refund_comp = _completeness(cur_df.get("refund_rate_pct", pd.Series(dtype=float)), analysis_days)
        margin_comp = _completeness(cur_df.get("margin_pct", pd.Series(dtype=float)), analysis_days)

        # ── FBA row (needed for price resolution and inventory signals) ────
        fba_row = fba_lookup.get((brand, asin))
        days_of_supply = float(fba_row["days_of_supply"]) if fba_row is not None and "days_of_supply" in fba_row.index and pd.notna(fba_row.get("days_of_supply")) else None
        inventory_score = _score_inventory_pressure(days_of_supply, thresholds["low_dos_days"])

        # ── Current price resolution ────────────────────────────────────────
        # Priority:
        #   1. Sellerise prod_sales_per_unit (recent 7-day avg) — actual realized
        #      price from live transactions, daily-updated, reflects promos/coupons.
        #   2. FBA your_price — static inventory snapshot. Used when Sellerise has
        #      no recent sales (e.g. dormant ASIN with stock still showing).
        #   3. None → HOLD
        #
        # Sellerise is primary because it comes from actual transactions and is
        # refreshed daily. FBA your_price is a periodic snapshot that takes the
        # median across SKU variants and can lag behind price changes.

        # Candidate 1: Sellerise price from actual sale days only.
        # prod_sales_per_unit = 0.0 on days with no sales — those are NOT "$0 prices",
        # they are no-data days. Including them in the average massively understates
        # price for slow-moving products (e.g. $19.99 × 2 sale days / 7 calendar days
        # = $5.71). Fix: filter to sale days first, then take the last 7 of those.
        # Median over mean: handles one-off promo/bundle prices without distortion.
        sellerise_price = None
        if "prod_sales_per_unit" in cur_df.columns and not cur_df.empty:
            sold_df = cur_df[cur_df["units"] > 0] if "units" in cur_df.columns else cur_df
            if not sold_df.empty:
                recent_sales = sold_df.tail(7)   # last 7 actual sale days, not calendar days
                non_zero = recent_sales["prod_sales_per_unit"]
                non_zero = non_zero[non_zero > 0]
                sellerise_price = float(non_zero.median()) if not non_zero.empty else None

        # Candidate 2: FBA your_price
        fba_price = None
        if fba_row is not None:
            raw_fba = fba_row.get("your_price")
            if raw_fba is not None and pd.notna(raw_fba) and float(raw_fba) > 0:
                fba_price = float(raw_fba)

        # Resolve with cross-check
        if sellerise_price and sellerise_price >= 2.0:
            current_price = sellerise_price
            price_source = "sellerise"
            if fba_price and abs(sellerise_price - fba_price) / fba_price > 0.30:
                logger.warning(
                    "ASIN %s price divergence: Sellerise %.2f vs FBA %.2f (>30%%) — using Sellerise",
                    asin, sellerise_price, fba_price,
                )
        elif fba_price:
            current_price = fba_price
            price_source = "fba"
            if sellerise_price and sellerise_price < 2.0:
                logger.warning(
                    "ASIN %s Sellerise price $%.2f < $2 (likely bad data), using FBA $%.2f",
                    asin, sellerise_price, fba_price,
                )
            else:
                logger.info(
                    "ASIN %s no recent Sellerise sales data — using FBA your_price $%.2f",
                    asin, fba_price,
                )
        else:
            current_price = None
            price_source = "missing"
            logger.warning("No price available for ASIN %s (%s)", asin, brand)

        # ── COGS resolution (fallback chain) ───────────────────────────────
        cogs_entry = cogs_lookup.get(asin)
        if cogs_entry and pd.notna(cogs_entry["cogs"]):
            cogs_val = cogs_entry["cogs"]
            cogs_source = "cogs_sheet"
        else:
            # Fallback: Sellerise CoG column
            sellerise_cog = _safe_mean(cur_df["cog"]) if "cog" in cur_df.columns else None
            if sellerise_cog is not None and not np.isnan(sellerise_cog):
                cogs_val = sellerise_cog
                cogs_source = "sellerise_cog"
                logger.debug("ASIN %s using Sellerise CoG fallback: %.2f", asin, cogs_val)
            else:
                cogs_val = None
                cogs_source = "missing"
                logger.warning("No COGS data for ASIN %s (%s)", asin, brand)

        # ── Sellerise CoG sanity check (now that price is known) ───────────────
        # Bug T2 fix: also reject CoG >= price (not just > 3× price).
        # Sellerise sometimes reports total account CoG or period CoG not per-unit.
        # If CoG >= price: margin is impossible before FBA/referral → bad data.
        # If CoG > 3× price: likely an aggregate/period total → bad data.
        # Force to missing so recommendation.py correctly HOLDs the ASIN.
        if cogs_source == "sellerise_cog" and current_price and cogs_val is not None \
                and (cogs_val >= current_price or cogs_val > current_price * 3):
            logger.warning(
                "ASIN %s Sellerise CoG %.2f vs price %.2f — likely bad COGS entry "
                "(exceeds price or >3× price), forcing HOLD",
                asin, cogs_val, current_price,
            )
            cogs_val = None
            cogs_source = "bad_ratio"   # distinct from "missing" — bad entry, not absent

        # ── Derived fee metrics ─────────────────────────────────────────────
        avg_referral_rate = None
        if "referral_fees" in cur_df.columns and "sales" in cur_df.columns:
            total_referral = cur_df["referral_fees"].sum()
            total_sales = cur_df["sales"].sum()
            if total_sales and total_sales > 0:
                avg_referral_rate = total_referral / total_sales

        if avg_referral_rate is None or np.isnan(avg_referral_rate) or avg_referral_rate <= 0:
            avg_referral_rate = 0.15
            logger.debug("ASIN %s using fallback referral rate 0.15", asin)

        avg_fba_fee_per_unit = None
        if "fba_fees" in cur_df.columns and "units" in cur_df.columns:
            total_fba = cur_df["fba_fees"].sum()
            total_units = cur_df["units"].sum()
            if total_units and total_units > 0:
                avg_fba_fee_per_unit = total_fba / total_units

        if avg_fba_fee_per_unit is None or np.isnan(avg_fba_fee_per_unit):
            avg_fba_fee_per_unit = 0.0

        # Fee Preview override: Amazon's own fee estimate is more accurate than
        # Sellerise's aggregated fee ÷ units.  Use it when available for this ASIN.
        _fp_key = (brand, asin)
        if _fp_key in fee_preview_lookup and fee_preview_lookup[_fp_key] > 0:
            avg_fba_fee_per_unit = fee_preview_lookup[_fp_key]
            logger.debug("ASIN %s FBA fee overridden by Fee Preview: $%.2f", asin, avg_fba_fee_per_unit)

        # ── Current margin (structural) and contribution margin (after ads) ──
        # Bug T1 fix: current_margin is STRUCTURAL (price - COGS - FBA - referral).
        # The margin_floor in config is a structural floor — it guards against pricing
        # decisions that erode the product cost structure. Ad spend (TACoS) is a
        # separate marketing P&L line and must NOT be mixed into the floor check.
        #
        # Bug 3 fix (this session): margin SIGNAL now also uses structural current_margin,
        # not Sellerise margin_pct (which includes TACoS). Previously avg_margin from
        # Sellerise was post-TACoS, causing ad-heavy products to show a low margin signal
        # even when their structural margin was healthy. This directly contradicts T1.
        #
        # contribution_margin = structural_margin - avg_tacos_rate (informational only).
        # Dollar impact figures in margin_calculator.py DO include TACoS for accuracy.

        # TACoS from Sellerise — needed for contribution_margin (informational).
        # Bug T3 fix: use median of sales-days only, cap outliers at 200%.
        avg_tacos_rate = None
        if "tacos_pct" in cur_df.columns:
            _tacos_col = cur_df["tacos_pct"]
            if "units" in cur_df.columns:
                _tacos_col = cur_df.loc[cur_df["units"] > 0, "tacos_pct"]
            tacos_vals = _tacos_col.dropna()
            tacos_vals = tacos_vals[tacos_vals <= 200]   # drop outlier days (>200% = noise)
            if len(tacos_vals) >= 3:
                raw_tacos = float(tacos_vals.median())
            elif len(tacos_vals) > 0:
                raw_tacos = float(tacos_vals.mean())
            else:
                raw_tacos = None
            if raw_tacos is not None and raw_tacos > 0:
                avg_tacos_rate = raw_tacos / 100.0

        # ── Storage cost per unit (monthly) ────────────────────────────────
        # Applied to contribution_margin only (not structural current_margin).
        _st_key = (brand, asin)
        monthly_storage_fee_per_unit = storage_lookup.get(_st_key, 0.0) or 0.0

        if current_price and current_price > 0 and cogs_val is not None:
            referral_fee = current_price * avg_referral_rate
            current_margin = (
                current_price - cogs_val - avg_fba_fee_per_unit - referral_fee
            ) / current_price
            # contribution_margin: structural − ads − storage holding cost
            storage_cost_pct = monthly_storage_fee_per_unit / current_price
            contribution_margin = current_margin - (avg_tacos_rate or 0.0) - storage_cost_pct
        else:
            current_margin = None
            contribution_margin = None
            monthly_storage_fee_per_unit = 0.0

        # Override margin completeness: if structural margin is not computable
        # (no price or COGS), zero the weight so it doesn't distort composite score.
        if current_margin is None:
            margin_comp = 0.0

        # ── Signal computation ──────────────────────────────────────────────
        # Bug 2 fix: filter to ad-active days (ctr_pct > 0) before computing per-ASIN CTR.
        # Zero-CTR days = no ads running (stockout or paused). Including them in the
        # mean artificially suppresses avg_ctr for products with coverage gaps.
        if "ctr_pct" in cur_df.columns:
            _ctr_active = cur_df.loc[cur_df["ctr_pct"] > 0, "ctr_pct"]
            avg_ctr = float(_ctr_active.mean()) if not _ctr_active.empty else None
        else:
            avg_ctr = None
        avg_cr = _safe_mean(cur_df["conversion_pct"]) if "conversion_pct" in cur_df.columns else None
        baseline_cr = _safe_mean(baseline_df["conversion_pct"]) if "conversion_pct" in baseline_df.columns and not baseline_df.empty else None
        avg_units = _safe_mean(cur_df["units"]) if "units" in cur_df.columns else None
        prior_units = _safe_mean(prior_df["units"]) if "units" in prior_df.columns and not prior_df.empty else None
        # avg_margin_pct kept for informational display in Full Catalog tab only.
        # It is NOT used for the margin signal — see Bug 3 fix above.
        avg_margin = _safe_mean(cur_df["margin_pct"]) if "margin_pct" in cur_df.columns else None
        # Sellerise always stores Margin as a percentage value (e.g. 21.68 = 21.68%)
        if avg_margin is not None:
            avg_margin = avg_margin / 100.0
        avg_refund = _safe_mean(cur_df["refund_rate_pct"]) if "refund_rate_pct" in cur_df.columns else None

        yoy_factor = _compute_yoy_factor(cur_df, yoy_df) if seasonal else None

        # Scores
        ctr_score = _score_ctr(avg_ctr, brand_avg_ctr.get(brand), thresholds)
        conversion_score = _score_conversion(avg_cr, baseline_cr, thresholds)
        velocity_score = _score_velocity(avg_units, prior_units, yoy_factor, seasonal, thresholds)
        # Bug 3 fix: use structural current_margin (not Sellerise margin_pct which includes TACoS).
        margin_score = _score_margin(current_margin, margin_floor, thresholds)

        # Trend (linear regression on net_profit per unit)
        # Bug 5 fix: apply per-unit normalization for ALL brands (was seasonal-only).
        # Total net_profit rises with volume even if per-unit profitability is flat or
        # declining — that volume signal belongs to velocity, not trend. Trend should
        # answer: "Is each unit generating more/less profit over time?"
        if "net_profit" in cur_df.columns and not cur_df.empty:
            day_indices = list(range(len(cur_df)))
            if "units" in cur_df.columns:
                _trend_units = cur_df["units"].replace(0, np.nan)
                profit_series = (cur_df["net_profit"] / _trend_units).ffill()
                profits = profit_series.tolist()
            else:
                profits = cur_df["net_profit"].tolist()
            trend_score, trend_p = _score_trend(day_indices, profits)
        else:
            trend_score, trend_p = 0.0, 1.0

        # ── Refund score — weighted by price-related fraction ──────────────
        # Raw refund rate may include returns driven by quality, FC damage, or wrong
        # choice — not by price.  When return data is available, scale the refund
        # score by the fraction that is actually price-related.
        # price_related_fraction < 0.20 → returns are NOT a pricing issue → halve signal
        # price_related_fraction >= 0.50 → returns strongly price-driven → full signal
        # No return reason data → use unmodified refund_score (existing behaviour)
        _raw_refund_score = _score_refund_rate(avg_refund, thresholds["refund_rate_threshold"])
        _ret_key = (brand, asin)
        if _ret_key in return_lookup:
            prf = return_lookup[_ret_key]
            if prf < 0.20:
                # Non-price returns dominate — dampen signal
                refund_score = _raw_refund_score * 0.5
            elif prf >= 0.50:
                refund_score = _raw_refund_score  # fully price-driven
            else:
                # Blend linearly between 0.5× and 1× for fractions 0.20–0.50
                blend = (prf - 0.20) / 0.30
                refund_score = _raw_refund_score * (0.5 + 0.5 * blend)
        else:
            refund_score = _raw_refund_score

        # ── 3PL buffer: clear inventory urgency when 3PL has available stock ──
        # inventory_score guards against cutting price when stock is low (risk of
        # stockout at reduced price).  If 3PL has buffer stock the FBA pipeline can
        # be replenished — remove the urgency flag.
        tpl_available = tpl_lookup.get(asin, 0.0)
        if tpl_available > 0 and inventory_score > 0:
            inventory_score = 0.0

        # ── BSR / keyword rank score (Helium10) ────────────────────────────
        h10_row = helium10_lookup.get(asin)
        rank_trend = float(h10_row["keyword_avg_rank_trend"]) \
            if h10_row is not None and pd.notna(h10_row.get("keyword_avg_rank_trend")) \
            else None
        bsr_score = _score_bsr(rank_trend)

        # ── Weights (with YoY reallocation for non-seasonal brands) ────────
        weights = dict(config["signals"]["weights"])
        if not seasonal:
            weights["velocity"] = weights.get("velocity", 0.20) + weights.pop("yoy_factor", 0.05)
            weights["yoy_factor"] = 0.0

        # Apply completeness zeroing to weights
        def _effective_weight(key: float, comp: float) -> float:
            return 0.0 if comp < min_completeness else key

        # For seasonal brands score yoy_factor as a real signal so the 0.05 weight
        # is not dead weight in the composite denominator.
        # Non-seasonal: weight already zeroed above, score stays 0.0.
        yoy_score = _score_yoy(yoy_factor) if seasonal else 0.0
        signal_scores = {
            "ctr": ctr_score,
            "conversion": conversion_score,
            "velocity": velocity_score,
            "margin": margin_score,
            "trend": trend_score,
            "refund_rate": refund_score,
            "inventory_pressure": inventory_score,
            "yoy_factor": yoy_score,
            "bsr": bsr_score,
        }
        completeness_map = {
            "ctr": ctr_comp,
            "conversion": cr_comp,
            "velocity": unit_comp,
            "margin": margin_comp,   # zeroed above if structural margin not computable
            "trend": profit_comp,
            "refund_rate": refund_comp,
            "inventory_pressure": 1.0,  # always available from FBA
            "yoy_factor": 1.0,
            "bsr": 1.0 if h10_row is not None else 0.0,  # available only when H10 data loaded
        }

        effective_weights = {
            k: _effective_weight(weights.get(k, 0.0), completeness_map.get(k, 1.0))
            for k in signal_scores
        }
        total_weight = sum(effective_weights.values())
        if total_weight > 0:
            composite_score = sum(signal_scores[k] * effective_weights[k]
                                  for k in signal_scores) / total_weight
        else:
            composite_score = 0.0

        # Bug 4 fix: avg_weekly_units — Sellerise primary, FBA fallback.
        # Velocity SIGNAL uses Sellerise avg_units (anchor-date-aligned 30d window).
        # Dollar IMPACT must use the same data source to stay consistent with the
        # signal that triggered the recommendation. FBA units_shipped_t30 comes from
        # a snapshot that may be weeks older than the Sellerise anchor date.
        if avg_units and avg_units > 0:
            avg_weekly_units = avg_units * 7
        elif fba_row is not None and "units_shipped_t30" in fba_row.index:
            units_t30 = fba_row.get("units_shipped_t30")
            avg_weekly_units = float(units_t30) / 4.3 if pd.notna(units_t30) and units_t30 > 0 else None
        else:
            avg_weekly_units = None

        # Overall data completeness (average across primary signals)
        data_completeness = np.mean([ctr_comp, cr_comp, unit_comp, margin_comp])

        # ── Product family ──────────────────────────────────────────────────
        family_info = family_lookup.get(asin, {})

        # ── Pre-change baselines ────────────────────────────────────────────
        pre_net_profit_weekly = None
        if "net_profit" in cur_df.columns:
            total_profit = cur_df["net_profit"].sum()
            days_in_window = (anchor_date - current_start).days or analysis_days
            weeks = days_in_window / 7.0
            pre_net_profit_weekly = total_profit / weeks if weeks > 0 else None

        pre_units_weekly = avg_weekly_units

        # Product title from Sellerise (take first non-null in window)
        asin_title = None
        if "title" in asin_df.columns:
            non_null_titles = asin_df["title"].dropna()
            if not non_null_titles.empty:
                asin_title = str(non_null_titles.iloc[0])

        # COGS product name — cleaner/shorter than Sellerise listing title.
        # Format: "Spa Clarifer - 1 Pint" (pack info already embedded in COGS name)
        _cogs_entry = cogs_lookup.get(asin, {})
        _cogs_pname = _cogs_entry.get("product_name")
        cogs_name = str(_cogs_pname).strip() if _cogs_pname and pd.notna(_cogs_pname) else None

        row_data = {
            # Identity
            "asin": asin,
            "brand": brand,
            "sub_brand": sub_brand,
            "effective_brand": effective_brand,
            "title": asin_title,
            "cogs_name": cogs_name,   # clean product name from COGS sheet (primary display name)
            "product_category": family_info.get("product_category"),
            "product_family": family_info.get("product_family"),
            "anchor_date": anchor_date.date(),
            # Pricing
            "current_price": current_price,
            "price_source": price_source,
            "cogs": cogs_val,
            "cogs_source": cogs_source,
            "current_margin": current_margin,           # structural: (price-cogs-fba-ref)/price
            "contribution_margin": contribution_margin, # after-ads: structural - avg_tacos_rate
            # Fee metrics
            "avg_referral_rate": avg_referral_rate,
            "avg_fba_fee_per_unit": avg_fba_fee_per_unit,
            "avg_tacos_rate": avg_tacos_rate,
            "avg_weekly_units": avg_weekly_units,
            # Inventory
            "days_of_supply": days_of_supply,
            "sales_rank": float(fba_row["sales_rank"]) if fba_row is not None and "sales_rank" in fba_row.index and pd.notna(fba_row.get("sales_rank")) else None,
            # Signal scores
            "ctr_score": ctr_score,
            "conversion_score": conversion_score,
            "velocity_score": velocity_score,
            "margin_score": margin_score,
            "trend_score": trend_score,
            "trend_p_value": trend_p,
            "refund_score": refund_score,
            "inventory_score": inventory_score,
            "bsr_score": bsr_score,
            "yoy_factor": yoy_factor,
            "composite_score": composite_score,
            "data_completeness": data_completeness,
            # Signal raw values (for reasoning strings)
            "avg_ctr": avg_ctr,
            "brand_avg_ctr": brand_avg_ctr.get(brand),
            "avg_conversion": avg_cr,
            "baseline_conversion": baseline_cr,
            "avg_units_daily": avg_units,
            "prior_units_daily": prior_units,
            "avg_refund_rate": avg_refund,
            "avg_margin_pct": avg_margin,
            "margin_floor": margin_floor,
            "seasonal": seasonal,
            # Supplementary signal metadata
            "keyword_avg_rank_trend": rank_trend,
            "tpl_available": tpl_available if tpl_available > 0 else None,
            "monthly_storage_fee_per_unit": monthly_storage_fee_per_unit if monthly_storage_fee_per_unit > 0 else None,
            "price_related_return_fraction": return_lookup.get((brand, asin)),
            # Pre-change baselines for History tab
            "pre_conversion": avg_cr,
            "pre_units_weekly": pre_units_weekly,
            "pre_net_profit_weekly": pre_net_profit_weekly,
            "pre_margin_pct": current_margin,
            "pre_ctr": avg_ctr,
            "pre_refund_rate": avg_refund,
            "pre_days_of_supply": days_of_supply,
        }
        rows.append(row_data)

    if not rows:
        logger.error("No signal rows computed")
        return pd.DataFrame()

    signals_df = pd.DataFrame(rows)
    logger.info("Signals computed for %d ASINs", len(signals_df))
    return signals_df
