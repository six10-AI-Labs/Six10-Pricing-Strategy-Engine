"""
main.py — CLI orchestrator for the Six10 Pricing Strategy Engine.

Usage:
  python main.py                            # full run (Sheets + email)
  python main.py --dry-run                  # compute only, no writes/email
  python main.py --no-email                 # Sheets write, no email
  python main.py --output-local OUT.xlsx    # write to local Excel instead of Sheets
  python main.py --dry-run --output-local test_output.xlsx
"""

import argparse
import logging
import sys
from pathlib import Path

import yaml
import pandas as pd

try:
    from dotenv import load_dotenv
    load_dotenv(override=True)
except ImportError:
    pass

# ── Logging setup ──────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler("pricing_engine.log", mode="a", encoding="utf-8"),
    ],
)
# Suppress noisy third-party library info logs
logging.getLogger("googleapiclient.discovery_cache").setLevel(logging.ERROR)
logging.getLogger("googleapiclient.discovery").setLevel(logging.WARNING)
logger = logging.getLogger("main")


def load_config(config_path: str = "config.yaml") -> dict:
    with open(config_path, "r", encoding="utf-8") as f:
        cfg = yaml.safe_load(f)
    return cfg


def _build_data_quality_report(
    recommendations_df: pd.DataFrame,
    biz_report_df,
    anchor_date_str: str,
    run_date: str,
    sellerise_brands: list,
    fba_count: int,
) -> dict:
    """Collect data quality issues from this run to surface in the email."""
    issues = []

    anchor_ts    = pd.Timestamp(anchor_date_str)
    run_ts       = pd.Timestamp(run_date)
    data_age_days = int((run_ts - anchor_ts).days)

    df = recommendations_df.copy()

    # ── COGS missing (genuinely absent from COGS sheet) ───────────────────────
    missing_cogs_df = df[df.get("cogs_source", pd.Series(dtype=str)) == "missing"]
    if not missing_cogs_df.empty:
        asins = missing_cogs_df["asin"].tolist()
        issues.append({
            "severity": "warning",
            "message":  (
                f"{len(asins)} ASIN(s) on HOLD — no COGS data found. "
                "Recommendations are locked until cost entries are added."
            ),
            "fix":   "Open <strong>COGS Sheet.xlsx</strong> (Data Feeds/) and add the per-unit cost for each ASIN below.",
            "asins": asins,
        })

    # ── COGS bad ratio (entry exists but looks wrong — likely total not per-unit) ──
    bad_ratio_df = df[df.get("cogs_source", pd.Series(dtype=str)) == "bad_ratio"]
    if not bad_ratio_df.empty:
        asins = bad_ratio_df["asin"].tolist()
        issues.append({
            "severity": "warning",
            "message":  (
                f"{len(asins)} ASIN(s) on HOLD — COGS entry looks incorrect "
                "(recorded cost is more than 3× the current sale price). "
                "This is usually a total-period cost entered instead of per-unit cost."
            ),
            "fix":   "Open <strong>COGS Sheet.xlsx</strong> and verify the cost is <em>per unit</em>, not a total or period value.",
            "asins": asins,
        })

    # ── No price data at all ──────────────────────────────────────────────────
    no_price_df = df[df.get("price_source", pd.Series(dtype=str)) == "missing"]
    if not no_price_df.empty:
        asins = no_price_df["asin"].tolist()
        issues.append({
            "severity": "warning",
            "message":  (
                f"{len(asins)} ASIN(s) have no price data — no recent Sellerise sales "
                "and no FBA listing price found. Confidence is very low."
            ),
            "fix":   "Check whether these ASINs are still active on Amazon. If yes, ensure they appear in the Sellerise export.",
            "asins": asins,
        })

    # ── FBA fallback prices (using snapshot, not live sales) ──────────────────
    fba_fallback_df = df[df.get("price_source", pd.Series(dtype=str)) == "fba"]
    if not fba_fallback_df.empty:
        asins = fba_fallback_df["asin"].tolist()
        issues.append({
            "severity": "info",
            "message":  (
                f"{len(asins)} ASIN(s) are using the FBA inventory snapshot price "
                "(no recent Sellerise sales recorded). These may be slow movers or newly launched."
            ),
            "fix":   None,   # Info only — no action required
            "asins": asins,
        })

    # ── Stale Sellerise data ──────────────────────────────────────────────────
    if data_age_days > 10:
        issues.append({
            "severity": "warning",
            "message":  (
                f"Sellerise data is <strong>{data_age_days} days old</strong> "
                f"(most recent date in files: {anchor_date_str}). "
                "Recommendations are based on this older snapshot."
            ),
            "fix":   "Export fresh data from <strong>Sellerise → Reports</strong> for all brands and replace the files in <strong>Data Feeds/</strong>.",
            "asins": [],
        })

    # Business Report is intentionally not used — Sellerise CTR serves as the
    # ad-performance proxy for all brands. Removed from issues list (not a data gap).

    return {
        "anchor_date":   anchor_date_str,
        "run_date":      run_date,
        "data_age_days": data_age_days,
        "sellerise_brands_loaded": sellerise_brands,
        "fba_count":     fba_count,
        "biz_report_status": "not_used",
        "issues":        issues,
        "has_warnings":  any(i["severity"] == "warning" for i in issues),
        "has_critical":  any(i["severity"] == "critical" for i in issues),
    }


def main():
    parser = argparse.ArgumentParser(description="Six10 Pricing Strategy Engine")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Compute signals and recommendations but do not write to Sheets or send email",
    )
    parser.add_argument(
        "--no-email",
        action="store_true",
        help="Write to Sheets but skip sending the weekly email",
    )
    parser.add_argument(
        "--email-only",
        action="store_true",
        help="Send email but skip writing to Google Sheets (useful for testing email layout)",
    )
    parser.add_argument(
        "--output-local",
        metavar="FILEPATH",
        help="Write output to a local Excel file instead of (or in addition to) Sheets",
    )
    parser.add_argument(
        "--config",
        default="config.yaml",
        metavar="PATH",
        help="Path to config.yaml (default: config.yaml)",
    )
    args = parser.parse_args()

    # ── Step 1: Load config ────────────────────────────────────────────────────
    logger.info("Loading config from %s", args.config)
    config = load_config(args.config)

    # ── Step 1b: Sync latest files from Google Drive ───────────────────────────
    # Downloads any Sellerise / FBA Inventory files added to Drive since the last
    # run, and always re-exports the COGS Sheet so cost edits are picked up.
    # Failures are logged but never abort the pipeline.
    try:
        from src.drive_sync import sync_data_feeds
        sync_data_feeds(config)
    except Exception as _sync_err:
        logger.warning("Drive sync raised an unexpected error — continuing: %s", _sync_err)

    # ── Step 2: Load all data sources ─────────────────────────────────────────
    logger.info("Loading data sources...")
    from src.data_loader import build_master_dataset
    (sellerise_df, fba_df, cogs_df, families_df, biz_report_df,
     helium10_df, fee_preview_df, storage_df, return_df, tpl_df) = build_master_dataset(config)

    if sellerise_df.empty:
        logger.error("No Sellerise data loaded — aborting")
        sys.exit(1)

    anchor_date = sellerise_df["date"].max()
    anchor_date_str = str(anchor_date.date()) if hasattr(anchor_date, "date") else str(anchor_date)[:10]
    run_date = pd.Timestamp.today().strftime("%Y-%m-%d")
    logger.info("Anchor date: %s | Run date: %s", anchor_date_str, run_date)

    # ── Step 2b: Read excluded ASINs (Full Catalog checkbox + Dismissed tab) ──
    from src.sheets_writer import (
        read_excluded_asins_from_sheets, read_dismissed_asins_tab,
        read_pipeline_actions_tab, backfill_implemented_dates_from_pipeline,
    )
    excluded_asins: set = set()
    if not args.dry_run:
        _fc_excluded   = read_excluded_asins_from_sheets(config)
        _tab_dismissed = read_dismissed_asins_tab(config)
        excluded_asins = _fc_excluded | _tab_dismissed
        if excluded_asins:
            logger.info(
                "Loaded %d excluded ASIN(s) total: %d from Full Catalog checkbox, %d from Dismissed tab",
                len(excluded_asins), len(_fc_excluded), len(_tab_dismissed),
            )

    # ── Step 3: Compute signals ────────────────────────────────────────────────
    logger.info("Computing signals...")
    from src.signal_computer import compute_signals
    signals_df = compute_signals(
        sellerise_df, fba_df, cogs_df, families_df, config,
        helium10_df=helium10_df,
        fee_preview_df=fee_preview_df,
        storage_df=storage_df,
        return_df=return_df,
        tpl_df=tpl_df,
    )

    if signals_df.empty:
        logger.error("No signals computed -- aborting")
        sys.exit(1)

    logger.info("Signals computed for %d ASINs", len(signals_df))

    # ── Step 3b: Split excluded ASINs out of the active pipeline ──────────────
    excluded_df = pd.DataFrame()
    if excluded_asins:
        exclude_mask = signals_df.apply(
            lambda r: (str(r.get("brand", "")), str(r.get("asin", ""))) in excluded_asins,
            axis=1,
        )
        excluded_df = signals_df[exclude_mask].copy()
        excluded_df["recommendation"] = "EXCLUDED"
        excluded_df["confidence"] = ""
        excluded_df["reasoning"] = "Manually excluded via Full Catalog checkbox"
        excluded_df["composite_score"] = None
        excluded_df["exclude"] = True
        signals_df = signals_df[~exclude_mask].copy()
        logger.info(
            "Excluded %d ASIN(s) from recommendations; %d active",
            len(excluded_df), len(signals_df),
        )

    # ── Step 4: Generate recommendations ──────────────────────────────────────
    logger.info("Generating recommendations...")
    from src.recommendation import generate_recommendations
    recommendations_df = generate_recommendations(signals_df, config)

    if recommendations_df.empty:
        logger.error("No recommendations generated — aborting")
        sys.exit(1)

    # ── Step 5: Calculate margin impact ───────────────────────────────────────
    logger.info("Calculating margin impact...")
    from src.margin_calculator import calculate_all_margin_impacts
    recommendations_df = calculate_all_margin_impacts(recommendations_df, config)

    # ── Step 5a: Reclassify LOWER → LOWER_VELOCITY_DEFENCE ───────────────────────
    # LOWERs where even the 5% cut is net-negative after elasticity-modelled volume
    # uplift go to a separate label so Excel and email are consistent.
    _vd_mask = (
        (recommendations_df["recommendation"] == "LOWER") &
        ~(recommendations_df["lower_5pct_scenario_b"].apply(
            lambda x: isinstance(x, (int, float)) and not pd.isna(x) and x > 0
        ))
    )
    recommendations_df.loc[_vd_mask, "recommendation"] = "LOWER_VELOCITY_DEFENCE"
    logger.info(
        "Reclassified %d LOWER -> LOWER_VELOCITY_DEFENCE (net-negative after elasticity)",
        int(_vd_mask.sum()),
    )

    # ── Step 5b: Re-evaluate margin_below_floor after margin_calculator ──────────
    # margin_below_floor is first set in recommendation.py (Step 4) from signals_df
    # current_margin.  Margin_calculator (Step 5) may refine current_margin for edge
    # cases (e.g. products with no Sellerise margin data).  Re-evaluate here so the
    # Critical Margin Alerts section in the email catches ALL products below their
    # floor regardless of recommendation type (including RAISE with negative margin).
    if "current_margin" in recommendations_df.columns and "margin_floor" in recommendations_df.columns:
        import math as _math
        def _recompute_mbf(row):
            cm = row.get("current_margin")
            mf = row.get("margin_floor")
            if cm is None or mf is None:
                return False
            try:
                cm_f = float(cm)
                mf_f = float(mf)
                return (not _math.isnan(cm_f)) and cm_f < mf_f
            except (TypeError, ValueError):
                return False
        updated_mbf = recommendations_df.apply(_recompute_mbf, axis=1)
        newly_flagged = int((updated_mbf & ~recommendations_df["margin_below_floor"].astype(bool)).sum())
        recommendations_df["margin_below_floor"] = updated_mbf
        if newly_flagged:
            logger.info(
                "Step 5b: %d additional ASIN(s) flagged margin_below_floor after margin_calculator refinement",
                newly_flagged,
            )

    # ── Step 5c: Low-volume guard ─────────────────────────────────────────────────
    # Fewer than min_weekly_units sales/week = insufficient data for reliable signals.
    # Signal windows are 30 days; < 3 units/week = fewer than ~13 sale events → noise.
    _min_units = config.get("signals", {}).get("min_weekly_units", 3)

    def _is_low_vol(x) -> bool:
        if x is None:
            return True
        try:
            return float(x) < _min_units
        except (TypeError, ValueError):
            return True

    _low_vol_mask = (
        recommendations_df["recommendation"].isin(
            ["RAISE", "LOWER", "LOWER_VELOCITY_DEFENCE"]
        )
        & recommendations_df["avg_weekly_units"].apply(_is_low_vol)
    )
    if _low_vol_mask.any():
        recommendations_df.loc[_low_vol_mask, "recommendation"] = "HOLD"
        recommendations_df.loc[_low_vol_mask, "confidence"]     = "Insufficient Data"
        logger.info(
            "Forced %d low-volume ASINs (< %.0f units/wk) to HOLD with 'Insufficient Data' confidence",
            int(_low_vol_mask.sum()), _min_units,
        )

    # ── Step 5.5: LLM enrichment (after margin calc so $ impacts are available) ──
    exec_summary = None
    if config.get("llm", {}).get("enabled", True):
        logger.info("Running LLM enrichment layer...")
        try:
            from src.llm_layer import LLMLayer
            llm_layer = LLMLayer(config, cogs_df, families_df)
            recommendations_df, exec_summary = llm_layer.enrich(recommendations_df)
        except Exception as exc:
            logger.warning(
                "LLM layer failed -- continuing without enrichment: %s", str(exc)[:200]
            )

    # Mark active recommendations as not excluded, then merge excluded rows back
    # for Full Catalog display (excluded rows carry no margin scenarios)
    recommendations_df["exclude"] = False
    if not excluded_df.empty:
        recommendations_df = pd.concat(
            [recommendations_df, excluded_df], ignore_index=True, sort=False
        )

    # ── Step 6: Look-back (read existing History, run look-back, get updated df) ──
    history_df = None
    if not args.dry_run:
        logger.info("Reading History tab for look-back analysis...")
        from src.sheets_writer import read_history_from_sheets
        from src.recommendation import run_lookback

        raw_history = read_history_from_sheets(config)
        if not raw_history.empty:
            # Back-fill implemented_date + action notes from Pipeline Actions tab
            # so look-back triggers automatically when team uses the email form
            pipeline_actions_df = read_pipeline_actions_tab(config)
            if not pipeline_actions_df.empty:
                raw_history = backfill_implemented_dates_from_pipeline(
                    raw_history, pipeline_actions_df
                )
            history_df = run_lookback(raw_history, signals_df, anchor_date, config)
            revert_count = int(history_df.get("revert_flag", pd.Series(False)).astype(str).str.lower().isin(["true", "1"]).sum())
            logger.info("Look-back complete — %d revert flags set", revert_count)
        else:
            logger.info("No existing History rows — skipping look-back")

    # active_recs = non-excluded rows (used for email and History writes)
    active_recs = recommendations_df[recommendations_df["recommendation"] != "EXCLUDED"].copy()

    # ── Step 6.5: Build data quality report for email ─────────────────────────
    sellerise_brands = sorted(sellerise_df["brand"].dropna().unique().tolist())
    data_quality_report = _build_data_quality_report(
        recommendations_df=active_recs,
        biz_report_df=biz_report_df,
        anchor_date_str=anchor_date_str,
        run_date=run_date,
        sellerise_brands=sellerise_brands,
        fba_count=len(fba_df),
    )

    # ── Step 7: Write outputs ──────────────────────────────────────────────────
    if args.output_local:
        logger.info("Writing local Excel to %s", args.output_local)
        from src.sheets_writer import write_local_excel
        # Full catalog includes excluded rows; history/email use active only
        write_local_excel(recommendations_df, history_df, config, anchor_date_str, args.output_local)

    if not args.dry_run and not getattr(args, "email_only", False):
        from src.sheets_writer import write_sheets
        write_sheets(recommendations_df, history_df, config, anchor_date_str)

    # ── Step 8: Send email ─────────────────────────────────────────────────────
    if (not args.dry_run and not args.no_email) or getattr(args, "email_only", False):
        logger.info("Sending weekly email...")
        from src.email_reporter import send_weekly_email
        send_weekly_email(
            active_recs, history_df, config, run_date, anchor_date_str,
            exec_summary=exec_summary,
            data_quality_report=data_quality_report,
        )

    # ── Summary ────────────────────────────────────────────────────────────────
    rec_counts  = recommendations_df["recommendation"].value_counts()
    n_raise     = rec_counts.get("RAISE", 0)
    n_lower     = rec_counts.get("LOWER", 0)
    n_lower_vd  = rec_counts.get("LOWER_VELOCITY_DEFENCE", 0)
    n_hold      = rec_counts.get("HOLD", 0)
    n_blocked   = rec_counts.get("LOWER_BLOCKED", 0)
    n_excluded  = rec_counts.get("EXCLUDED", 0)
    n_total     = len(recommendations_df)

    print(
        f"\nRun complete. {n_total} ASINs | "
        f"{n_raise} RAISE | {n_lower} LOWER | {n_lower_vd} LOWER_VD | "
        f"{n_hold} HOLD | {n_blocked} BLOCKED"
        + (f" | {n_excluded} EXCLUDED" if n_excluded else "")
    )
    if args.dry_run:
        print("(dry-run: no data written to Sheets or email sent)")
    if args.output_local:
        print(f"Output written to: {args.output_local}")


if __name__ == "__main__":
    main()
