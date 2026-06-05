"""
email_reporter.py — Send weekly HTML pricing alert email via Gmail API.

Transport: Gmail API with OAuth 2.0 user credentials.
  - No SMTP, no App Password, no admin delegation required.
  - credentials-gmail-pricing.json  = OAuth client credentials (downloaded once from Google Cloud Console)
  - gmail_token.json                = stored OAuth token (auto-created by authorize_gmail.py)

Flow:
  1. Run authorize_gmail.py once — browser opens, log in as ai@six10ventures.com, grant Send permission
  2. gmail_token.json is saved — engine uses it on every subsequent run
  3. Token auto-refreshes silently when it expires (no re-auth needed)
"""

import base64
import logging
import os
import re
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path
from typing import Optional
from urllib.parse import urlencode

import pandas as pd

logger = logging.getLogger(__name__)

GMAIL_SCOPES = ["https://www.googleapis.com/auth/gmail.send"]
TEMPLATE_FILE = "email_template.html"

# Keywords that confirm a COGS product name includes explicit size/variant info.
# If none match, we prefer the Sellerise title (which often has the size from the
# Amazon listing) over the compact COGS name (which may omit size for bundles/kits).
_SIZE_KWS = (
    "oz", "pint", "quart", "gallon", "gal", " lb", "lbs", " g ", " kg",
    "ml", " ct", "count", "pack", "tablet", "strip", "-inch", "\" ",
    "3-in-1", "5lb", "10lb", "2lb", "1lb",  # common weight suffixes without space
)

# Regex patterns for extracting a compact size label from a COGS product name.
# Matches volume/weight measurements and pack counts.
_VOL_RE  = re.compile(
    # (?<![,\d]) prevents matching "000" in "12,000 Gallons" — the digit must not
    # be immediately preceded by a comma or another digit (i.e. it starts a fresh number).
    # ounces? added to catch "8.5 Ounces" style titles.
    r"(?<![,\d])(\d+(?:\.\d+)?)\s*(oz|fl\.?\s*oz|ounces?\b|pint|quart|gallon|gal\b|lbs?\b)",
    re.IGNORECASE,
)
# count/ct added to catch "50 Count", "30 ct" style product labels
_PACK_RE = re.compile(r"(\d+)\s*(?:pack|pk|count|ct)\b", re.IGNORECASE)


def _extract_size(text: Optional[str], structured: bool = True) -> Optional[str]:
    """Extract a compact size label from a product name string.

    Args:
        text:       The string to search (COGS name or Sellerise title).
        structured: True for COGS names (structured " - Size" format trusted).
                    False for Sellerise titles (only regex extraction; separator not trusted).

    Examples (COGS names, structured=True):
      "Spa Clarifer - 1 Pint"                → "1 Pint"
      "Spa Clarifer - 1 Pint- 3 Pack"        → "1 Pint- 3 Pack"
      "Spa Clarifer - 12 Pack (1 Pint each)" → "12 Pack (1 Pint each)"
      "Pool Clarifier 32oz"                  → "32oz"
      "MAV Pool Alkalinity Increaser - 5lb"  → "5lb"
      "Spa Enzyme 12 Pack"                   → "12 Pack"
      "Spa Calcium Increaser"                → None
      "VisiVite AREDS 2 - Eye Health Supplement"  → None  (no size keyword after " - ")

    Examples (Sellerise titles, structured=False):
      "AquaDoc Bromine Booster - 16 oz - Sodium Bromide..."  → "16 oz"  (stops at next " - ")
      "Non Chlorine Spa Shock for Hot tub 1 Pack"            → "1 Pack"
    """
    if not text:
        return None

    # Pattern 1 (COGS only): "Product Name - Size" — validated by size keyword presence.
    # Skipped for Sellerise titles because " - " there may separate subtitle sections.
    if structured and " - " in text:
        size_part = text.split(" - ", 1)[1].strip()
        if size_part and any(kw in size_part.lower() for kw in _SIZE_KWS):
            return size_part
        # Fall through — separator exists but nothing after it looks like a size

    # Pattern 2: Regex — return only the matched token(s), never "to end of string".
    # This prevents grabbing long product descriptions that happen to follow a size token.
    # When both vol and pack match AND are adjacent (gap ≤ 2 chars), combine them
    # so "32oz 2 Pack" stays together rather than returning just "32oz".
    vol_m  = _VOL_RE.search(text)
    pack_m = _PACK_RE.search(text)
    if vol_m and pack_m:
        first_end    = min(vol_m.end(), pack_m.end())
        second_start = max(vol_m.start(), pack_m.start())
        if second_start - first_end <= 2:
            # Adjacent — combine both tokens
            s = min(vol_m.start(), pack_m.start())
            e = max(vol_m.end(), pack_m.end())
            return text[s:e].strip() or None
        # Not adjacent — return just the volume measurement
        return vol_m.group(0).strip() or None
    if vol_m:
        return vol_m.group(0).strip() or None
    if pack_m:
        return pack_m.group(0).strip() or None

    return None

# ── Optional imports ───────────────────────────────────────────────────────────
try:
    from jinja2 import Environment, FileSystemLoader
    JINJA2_AVAILABLE = True
except ImportError:
    JINJA2_AVAILABLE = False
    logger.warning("jinja2 not installed — email sending disabled")

try:
    from google.oauth2.credentials import Credentials
    from google.auth.transport.requests import Request
    from googleapiclient.discovery import build
    from googleapiclient.errors import HttpError
    GMAIL_API_AVAILABLE = True
except ImportError:
    GMAIL_API_AVAILABLE = False
    logger.warning("google-api-python-client not installed — email sending disabled")


# ─────────────────────────────────────────────────────────────────────────────
# Auth helpers
# ─────────────────────────────────────────────────────────────────────────────

def _load_gmail_credentials(config: dict) -> Optional["Credentials"]:
    """Load OAuth token from gmail_token.json; refresh if expired.

    Returns None (with warning) if token file doesn't exist yet
    (user must run authorize_gmail.py first).
    """
    token_path = (
        os.environ.get("GMAIL_TOKEN_PATH")
        or config["email"].get("gmail_token_path", "gmail_token.json")
    )
    creds_path = (
        os.environ.get("GMAIL_CREDENTIALS_PATH")
        or config["email"].get("gmail_credentials_path", "credentials-gmail-pricing.json")
    )

    token_file = Path(token_path)
    if not token_file.exists():
        logger.warning(
            "gmail_token.json not found at '%s'. "
            "Run 'python authorize_gmail.py' once to complete OAuth consent, "
            "then re-run the engine.", token_path
        )
        return None

    creds = Credentials.from_authorized_user_file(str(token_file), GMAIL_SCOPES)

    # Refresh silently if expired (uses stored refresh_token, no browser needed)
    if not creds.valid:
        if creds.expired and creds.refresh_token:
            try:
                creds.refresh(Request())
                # Persist the refreshed token
                with open(token_file, "w") as f:
                    f.write(creds.to_json())
                logger.info("Gmail OAuth token refreshed and saved")
            except Exception as exc:
                logger.error(
                    "Failed to refresh Gmail token: %s. "
                    "Run 'python authorize_gmail.py' again to re-authorise.", exc
                )
                return None
        else:
            logger.error(
                "Gmail credentials are invalid and cannot be refreshed. "
                "Run 'python authorize_gmail.py' again."
            )
            return None

    return creds


# ─────────────────────────────────────────────────────────────────────────────
# Template helpers (unchanged from original)
# ─────────────────────────────────────────────────────────────────────────────

def _load_template_env() -> Optional[object]:
    if not JINJA2_AVAILABLE:
        return None
    template_dir = Path(__file__).parent
    return Environment(loader=FileSystemLoader(str(template_dir)), autoescape=True)


def _safe_float(val) -> Optional[float]:
    if val is None:
        return None
    if isinstance(val, float) and pd.isna(val):
        return None
    if isinstance(val, str) and val.strip() == "":
        return None
    try:
        return float(val)
    except (TypeError, ValueError):
        return None


def _row_to_dict(row: pd.Series) -> dict:
    float_cols = [
        "current_price", "composite_score", "current_margin", "contribution_margin",
        "avg_weekly_units", "margin_floor",
        "raise_5pct_new_price", "raise_5pct_weekly_impact",
        "raise_10pct_new_price", "raise_10pct_weekly_impact",
        "lower_5pct_new_price", "lower_5pct_scenario_a", "lower_5pct_scenario_b",
        "lower_5pct_break_even_pct",
        "lower_10pct_new_price", "lower_10pct_scenario_a", "lower_10pct_scenario_b",
        "lower_10pct_break_even_pct",
        "pre_conversion", "post_conversion",
        "pre_net_profit_weekly", "post_net_profit_weekly",
    ]
    d = {}
    for k, v in row.items():
        d[k] = _safe_float(v) if k in float_cols else v
    d["lower_5pct_viable"] = bool(row.get("lower_5pct_viable"))
    d["lower_10pct_viable"] = bool(row.get("lower_10pct_viable"))
    # Normalize sub_brand: pandas NaN is truthy in Jinja2 and renders as "nan"
    sub = d.get("sub_brand")
    if sub is None or (isinstance(sub, float) and pd.isna(sub)) or str(sub).strip() in ("nan", "None", ""):
        d["sub_brand"] = None
    # Title priority: COGS product name (when it includes size) → Sellerise title (80 chars)
    # COGS names are compact: "Spa Clarifer - 1 Pint". But ~30% of entries omit size
    # (e.g. "Pool Phosphate Remover", "Spa Calcium Increaser"). In those cases the
    # Sellerise Amazon listing title is more likely to carry the size/variant, so we
    # prefer it to avoid Amber/Neil seeing size-free names in the email tables.
    cogs_name  = row.get("cogs_name")
    raw_title  = row.get("title")
    _cogs_str  = str(cogs_name).strip() if cogs_name and str(cogs_name).strip() not in ("nan", "None", "") else None
    _title_str = str(raw_title).strip() if raw_title and str(raw_title).strip() not in ("nan", "None", "") else None

    # ── Size extraction ───────────────────────────────────────────────────────
    # 1. Try COGS name first (structured: " - Size" format is reliable).
    # 2. Fall back to Sellerise title (unstructured: regex only, no separator trust).
    _size_cogs  = _extract_size(_cogs_str, structured=True)
    _size_title = _extract_size(_title_str, structured=False) if _title_str else None
    d["size"] = _size_cogs or _size_title

    # ── Title (title_short) ───────────────────────────────────────────────────
    # RULE: always prefer COGS name for display — it is always shorter and more
    # readable than the full Amazon listing title. The Sellerise title is used
    # ONLY for size extraction above, never as the display name.
    #
    # Size deduplication: when the size is also in the Product column it looks
    # redundant. Strip it from the title when we can do so cleanly:
    #   "Spa Stain & Scale - 1 Gallon" → "Spa Stain & Scale"   (separator split)
    #   "Ph Decreaser 32oz 2 Pack"     → "Ph Decreaser"         (suffix strip)
    #   "8 lb. Pet Friendly Ice Melt"  → unchanged              (mid-embedded, no clean split)
    if _cogs_str:
        if _size_cogs and " - " in _cogs_str:
            # Clean " - Size" separator — keep only the base name
            d["title_short"] = _cogs_str.split(" - ", 1)[0].strip()
        elif _size_cogs and _cogs_str.endswith(_size_cogs):
            # Size is a clean suffix — strip it
            base = _cogs_str[: -len(_size_cogs)].strip().rstrip("-").strip()
            d["title_short"] = base if base else _cogs_str
        else:
            # No clean split, or no size at all — use full COGS name as-is.
            # This is always shorter than the Amazon listing title.
            d["title_short"] = _cogs_str
    elif _title_str:
        # Last resort: no COGS entry for this ASIN at all
        d["title_short"] = _title_str
    else:
        d["title_short"] = None
    # reasoning_short: 350-char truncated version for table cells
    # The full `reasoning` field is preserved for Sheets writes
    raw_reasoning = str(d.get("reasoning", "") or "")
    if len(raw_reasoning) > 350:
        d["reasoning_short"] = raw_reasoning[:350].rstrip() + "\u2026"
    else:
        d["reasoning_short"] = raw_reasoning
    return d


def _confidence_rank(conf: str) -> int:
    return {"High": 3, "Medium": 2, "Low": 1}.get(str(conf), 0)


def _name_from_email(email: str) -> str:
    """Extract a display name from an email address.

    shashank@six10ventures.com  →  Shashank
    neil.smith@company.com      →  Neil Smith
    ai@six10ventures.com        →  Ai  (sender; won't appear in by= param)
    """
    local = email.split("@")[0].split("+")[0]   # drop alias suffix if any
    # Replace dots/underscores/hyphens with spaces, then title-case
    name = local.replace(".", " ").replace("_", " ").replace("-", " ").title()
    return name.strip()


def _action_url(base_url: str, action: str, asin: str, brand: str,
                title: str, rec: str, by: str = "") -> Optional[str]:
    """Build a Google Apps Script URL for 1-click Dismiss / Pipeline logging.

    Returns None when base_url is not configured — template guards with
    ``{% if r.dismiss_url %}`` so rows still render cleanly with '—'.

    `by` is the display name of the recipient this email was sent to.
    It is embedded in the URL so the Apps Script can log who clicked.
    """
    if not base_url:
        return None
    params_dict = {
        "action": action,
        "asin":   (asin  or "").strip(),
        "brand":  (brand or "").strip(),
        "title":  (title or "")[:60].strip(),
        "rec":    (rec   or "").strip(),
    }
    if by:
        params_dict["by"] = by
    return f"{base_url}?{urlencode(params_dict)}"


def _action_url_all(base_url: str, rows: list, by: str = "") -> Optional[str]:
    """Build a single bulk-approve URL for all top RAISE rows.

    Uses pipe-separated params so the Apps Script ``pipeline_all`` handler
    can log every ASIN in one click without any JavaScript on the email side.

    Returns None when base_url is not configured or rows is empty.
    """
    if not base_url or not rows:
        return None
    asins  = "|".join((r.get("asin",  "") or "")[:10].strip()  for r in rows)
    brands = "|".join((r.get("brand", "") or "")[:20].strip()  for r in rows)
    titles = "|".join(
        ((r.get("title_short") or r.get("title", "")) or "")[:40].strip()
        for r in rows
    )
    recs   = "|".join((r.get("recommendation", "") or "").strip() for r in rows)
    params_dict: dict = {
        "action": "pipeline_all",
        "asins":  asins,
        "brands": brands,
        "titles": titles,
        "recs":   recs,
    }
    if by:
        params_dict["by"] = by
    return f"{base_url}?{urlencode(params_dict)}"


# ─────────────────────────────────────────────────────────────────────────────
# Seasonality section builder
# ─────────────────────────────────────────────────────────────────────────────

def _build_seasonality_section(df: pd.DataFrame, config: Optional[dict]) -> dict:
    """Build seasonality status for all brands, to be rendered in the email.

    For seasonal brands: shows avg YoY ratio, number of ASINs with data, and
    interpretation (above/on-par/below last year's seasonal pace).
    For non-seasonal brands: confirms no adjustment applied.
    """
    if config is None:
        return {"seasonal_brands": [], "non_seasonal_brands": [], "has_seasonal": False}

    brands_config = config.get("brands", {})

    seasonal_status = []
    non_seasonal_present = []

    # Get effective_brand values present in this run's recommendations
    active_brands = set(df["effective_brand"].dropna().unique()) if "effective_brand" in df.columns else set()

    for brand_key, brand_cfg in brands_config.items():
        is_seasonal = brand_cfg.get("seasonal", False)
        brand_label = brand_key.replace("_", " ").title()

        if is_seasonal:
            # Collect YoY factors from all ASINs belonging to this brand
            brand_mask = df["effective_brand"] == brand_key
            brand_df = df[brand_mask]
            if brand_df.empty:
                continue

            yoy_vals = brand_df["yoy_factor"].dropna() if "yoy_factor" in brand_df.columns else pd.Series(dtype=float)
            asin_count = len(brand_df)

            if yoy_vals.empty:
                seasonal_status.append({
                    "brand": brand_label,
                    "asin_count": asin_count,
                    "yoy_count": 0,
                    "avg_yoy": None,
                    "status": "No prior-year data",
                    "interpretation": (
                        "Insufficient historical data to compute YoY ratio. "
                        "Velocity signal not seasonally adjusted this run."
                    ),
                    "status_color": "grey",
                    "asin_details": [],
                })
            else:
                avg_yoy = float(yoy_vals.mean())
                yoy_pct = (avg_yoy - 1.0) * 100
                if avg_yoy > 1.1:
                    status = f"Running ahead of last year (+{yoy_pct:.0f}%)"
                    interpretation = (
                        f"Current velocity is {avg_yoy:.2f}× last year's same-period pace. "
                        "Demand is seasonally stronger — RAISE signals given extra support."
                    )
                    status_color = "green"
                elif avg_yoy < 0.9:
                    status = f"Running behind last year ({yoy_pct:.0f}%)"
                    interpretation = (
                        f"Current velocity is {avg_yoy:.2f}× last year's same-period pace. "
                        "Velocity dips are expected in the off-season and are automatically "
                        "suppressed in the composite score — this is not a pricing failure."
                    )
                    status_color = "amber"
                else:
                    status = f"In line with last year ({yoy_pct:+.0f}%)"
                    interpretation = (
                        f"Current velocity is {avg_yoy:.2f}× last year's same-period. "
                        "Seasonal pattern tracking normally."
                    )
                    status_color = "green"

                asin_details = []
                for _, asin_row in brand_df.iterrows():
                    yf = asin_row.get("yoy_factor")
                    if pd.notna(yf):
                        trend = "hot" if float(yf) > 1.1 else ("cold" if float(yf) < 0.9 else "normal")
                        raw_title = asin_row.get("title") or asin_row.get("asin", "")
                        asin_details.append({
                            "asin": asin_row.get("asin", ""),
                            "title": str(raw_title)[:60],
                            "yoy_factor": round(float(yf), 2),
                            "trend": trend,
                            "recommendation": asin_row.get("recommendation", "HOLD"),
                        })
                asin_details.sort(key=lambda x: x["yoy_factor"], reverse=True)

                seasonal_status.append({
                    "brand": brand_label,
                    "asin_count": asin_count,
                    "yoy_count": int(len(yoy_vals)),
                    "avg_yoy": round(avg_yoy, 2),
                    "status": status,
                    "interpretation": interpretation,
                    "status_color": status_color,
                    "asin_details": asin_details,
                })
        else:
            # Non-seasonal: only list brands that have ASINs in this run
            if brand_key in active_brands:
                non_seasonal_present.append(brand_label)

    return {
        "seasonal_brands": seasonal_status,
        "non_seasonal_brands": non_seasonal_present,
        "has_seasonal": len(seasonal_status) > 0,
        "has_non_seasonal": len(non_seasonal_present) > 0,
    }


# ─────────────────────────────────────────────────────────────────────────────
# Per-row helper: plain-English signal summary
# ─────────────────────────────────────────────────────────────────────────────

def _build_signal_summary(row: dict) -> str:
    """One-line plain-English signal summary for a RAISE or LOWER row.

    Example output:
        "Click rate 65% below brand average · converting 11% worse than the
         90-day baseline · 31 units/week"
    Returns empty string when no meaningful signal deltas are present.
    """
    parts = []

    avg_ctr       = _safe_float(row.get("avg_ctr"))
    brand_avg_ctr = _safe_float(row.get("brand_avg_ctr"))
    avg_cr        = _safe_float(row.get("avg_conversion"))
    baseline_cr   = _safe_float(row.get("baseline_conversion"))
    avg_units     = _safe_float(row.get("avg_weekly_units"))

    if avg_ctr is not None and brand_avg_ctr and brand_avg_ctr > 0:
        delta = (avg_ctr - brand_avg_ctr) / brand_avg_ctr * 100
        if abs(delta) >= 10:
            direction = "below" if delta < 0 else "above"
            parts.append(f"Click rate {abs(delta):.0f}% {direction} brand average")

    if avg_cr is not None and baseline_cr and baseline_cr > 0:
        delta = (avg_cr - baseline_cr) / baseline_cr * 100
        if abs(delta) >= 5:
            direction = "worse than" if delta < 0 else "better than"
            parts.append(f"converting {abs(delta):.0f}% {direction} the 90-day baseline")

    if avg_units:
        parts.append(f"{avg_units:.0f} units/week")

    return " · ".join(parts)  # middle-dot separator


def _build_risk_inline(row: dict) -> str:
    """Compute inline risk warning text from raw signal values.

    Returns a short warning string (or empty string if no risks).
    Used to surface risk flags next to the affected ASIN in the email,
    rather than in a separate section.
    """
    warnings = []

    refund = _safe_float(row.get("avg_refund_rate"))
    if refund is not None and refund > 0.05:
        warnings.append(f"Refund rate {refund * 100:.1f}% above threshold")

    dos = _safe_float(row.get("days_of_supply"))
    if dos is not None and dos < 14:
        warnings.append(f"Low inventory ({dos:.0f} days supply)")

    cogs_src = str(row.get("cogs_source", "") or "")
    if cogs_src in ("missing", "bad_ratio"):
        warnings.append("COGS data unreliable — review before acting")

    return " | ".join(warnings)


def _build_performance_rows(
    history_df: Optional[pd.DataFrame],
    recommendations_df: pd.DataFrame,
    anchor_date: str,
) -> list:
    """Performance deltas for ASINs actioned in the last 2–4 weeks.

    Reads History rows where implemented_date is 14–28 days before
    anchor_date and computes delta vs current run metrics.

    Returns list of dicts for the template; empty list when no data.
    """
    if history_df is None or history_df.empty:
        return []

    anchor_ts = pd.Timestamp(anchor_date)
    h = history_df.copy()
    h["implemented_date"] = pd.to_datetime(h["implemented_date"], errors="coerce")

    # 2-to-4-week window (Neil: "last 2 to 4 weeks")
    mask = (
        h["implemented_date"].notna()
        & ((anchor_ts - h["implemented_date"]).dt.days >= 14)
        & ((anchor_ts - h["implemented_date"]).dt.days <= 28)
    )
    recent = h[mask].copy()
    if recent.empty:
        return []

    # One row per brand+asin (most recently implemented)
    recent = (
        recent.sort_values("implemented_date")
        .groupby(["brand", "asin"])
        .last()
        .reset_index()
    )

    # Index current-run metrics by (brand, asin)
    current_lookup: dict = {}
    for _, sig_row in recommendations_df.iterrows():
        key = (str(sig_row.get("brand", "")), str(sig_row.get("asin", "")))
        current_lookup[key] = sig_row

    rows = []
    for _, row in recent.iterrows():
        key = (str(row.get("brand", "")), str(row.get("asin", "")))
        cur = current_lookup.get(key)

        # Pre-change baselines (from History at time of recommendation)
        pre_units  = _safe_float(row.get("pre_units_weekly"))
        pre_margin = _safe_float(row.get("pre_margin_pct"))
        # Prefer actual implemented price; fall back to the recommended price at run time
        pre_price  = _safe_float(row.get("actual_price_implemented")) or _safe_float(row.get("current_price"))

        # Current-run values (pre_* in current run = current state this week)
        cur_units  = _safe_float(cur.get("avg_weekly_units"))  if cur is not None else None
        cur_margin = _safe_float(cur.get("current_margin"))    if cur is not None else None
        cur_price  = _safe_float(cur.get("current_price"))     if cur is not None else None

        delta_units_pct     = round((cur_units  - pre_units)  / pre_units  * 100, 1) if pre_units  and cur_units  else None
        delta_margin_pp     = round((cur_margin - pre_margin) * 100, 1)               if pre_margin is not None and cur_margin is not None else None
        delta_revenue_weekly = round(
            (cur_units * cur_price) - (pre_units * pre_price), 0
        ) if pre_units and pre_price and cur_units and cur_price else None

        outcome = str(row.get("outcome", "pending")).lower()
        rows.append({
            "asin":                  row.get("asin"),
            "brand":                 row.get("brand"),
            "title_short":           str(row.get("title", "") or "")[:60].strip() or None,
            "recommendation":        row.get("recommendation"),
            "implemented_date":      str(row.get("implemented_date", ""))[:10],
            "pre_price":             pre_price,
            "curr_price":            cur_price,
            "delta_units_pct":       delta_units_pct,
            "delta_margin_pp":       delta_margin_pp,
            "delta_revenue_weekly":  delta_revenue_weekly,
            "outcome":               outcome,
            "data_available":        cur is not None,
        })

    # Underperforming first (needs attention), then pending, then success
    rows.sort(key=lambda r: {"underperforming": 0, "pending": 1, "success": 2}.get(r["outcome"], 1))
    return rows


# ─────────────────────────────────────────────────────────────────────────────
# Context builder + renderer
# ─────────────────────────────────────────────────────────────────────────────

def build_email_context(
    recommendations_df: pd.DataFrame,
    history_df: Optional[pd.DataFrame],
    run_date: str,
    anchor_date: str,
    exec_summary: Optional[dict] = None,
    config: Optional[dict] = None,
    data_quality_report: Optional[dict] = None,
    recipient_name: str = "",
    also_sent_to: Optional[list] = None,
) -> dict:
    df = recommendations_df.copy()
    df["_cr"] = df.get("confidence", pd.Series(dtype=str)).apply(_confidence_rank)

    counts = {
        "RAISE":                  int((df["recommendation"] == "RAISE").sum()),
        "LOWER":                  int((df["recommendation"] == "LOWER").sum()),
        "LOWER_VELOCITY_DEFENCE": int((df["recommendation"] == "LOWER_VELOCITY_DEFENCE").sum()),
        "LOWER_BLOCKED":          int((df["recommendation"] == "LOWER_BLOCKED").sum()),
        "HOLD":                   int((df["recommendation"] == "HOLD").sum()),
        "total":                  len(df),
    }

    # ── Week-over-week delta ───────────────────────────────────────────────────
    wow_delta = {}
    if history_df is not None and not history_df.empty and "run_date" in history_df.columns:
        _prev_run = history_df[history_df["run_date"] != run_date]["run_date"].max()
        if pd.notna(_prev_run):
            _prev_df = history_df[history_df["run_date"] == _prev_run]
            wow_delta = {
                "RAISE":   int(((_prev_df["recommendation"] == "RAISE")).sum()),
                "LOWER":   int((_prev_df["recommendation"].isin(
                               ["LOWER", "LOWER_VELOCITY_DEFENCE"])).sum()),
                "HOLD":    int(((_prev_df["recommendation"] == "HOLD")).sum()),
                "BLOCKED": int(((_prev_df["recommendation"] == "LOWER_BLOCKED")).sum()),
            }

    # Review time estimate — based on actionable items (RAISE + profitable LOWER only)
    n_actions = counts["RAISE"] + counts["LOWER"]
    review_time_mins = 5 if n_actions <= 4 else (10 if n_actions <= 15 else 20)

    raise_rows = [
        _row_to_dict(row) for _, row in
        df[df["recommendation"] == "RAISE"]
        # Dollar impact primary; confidence breaks ties — surfaces highest-$ opportunity first
        .sort_values(["raise_5pct_weekly_impact", "_cr"], ascending=[False, False])
        .head(5).iterrows()
    ]
    # ── LOWER split — use pre-classified labels (set in main.py after margin calc) ──
    # LOWER                  = profitable cut (scenario_b > 0)   → green main section (top 5)
    # LOWER_VELOCITY_DEFENCE = net-negative cut (all scenarios ≤ 0) → reference footer (all)
    _viable    = df[df["recommendation"] == "LOWER"].sort_values(
        "lower_5pct_scenario_b", ascending=False
    )
    _nonviable = df[df["recommendation"] == "LOWER_VELOCITY_DEFENCE"].sort_values(
        "avg_weekly_units", ascending=False
    )

    lower_velocity_total = len(_nonviable)
    lower_no_data_count  = int(_nonviable["lower_5pct_scenario_b"].isna().sum()) if not _nonviable.empty else 0

    # Profitable LOWERs: top 5 for main action section (no longer merged with velocity-defence)
    lower_rows = []
    for _, row in _viable.head(5).iterrows():
        d = _row_to_dict(row)
        d["all_scenarios_net_negative"] = False
        lower_rows.append(d)

    # Velocity-defence LOWERs: ALL of them go to reference footer
    lower_velocity_rows = []
    for _, row in _nonviable.iterrows():
        d = _row_to_dict(row)
        d["all_scenarios_net_negative"] = True
        lower_velocity_rows.append(d)

    lower_velocity_shown = len(lower_velocity_rows)

    # Build velocity_watch: top 3 LOWER_VELOCITY_DEFENCE rows >= 50 units/wk (for exec summary)
    _velocity_watch_rows = []
    if not _nonviable.empty and "avg_weekly_units" in _nonviable.columns:
        _vw_candidates = _nonviable[
            _nonviable["avg_weekly_units"].apply(lambda x: (_safe_float(x) or 0.0) >= 50)
        ].sort_values("avg_weekly_units", ascending=False).head(3)
    else:
        _vw_candidates = pd.DataFrame()
    for _, vw_row in _vw_candidates.iterrows():
        _vw_d     = _row_to_dict(vw_row)
        _vw_units = _safe_float(vw_row.get("avg_weekly_units"))
        _vw_price = _safe_float(vw_row.get("current_price"))
        _vw_raw   = str(vw_row.get("reasoning", "") or "")
        _velocity_watch_rows.append({
            "asin":             vw_row.get("asin"),
            "title_short":      _vw_d.get("title_short"),
            "size":             _vw_d.get("size"),
            "brand":            vw_row.get("brand"),
            "avg_weekly_units": _vw_units,
            "current_price":    _vw_price,
            "reasoning_short":  _vw_raw[:350].rstrip() + "\u2026" if len(_vw_raw) > 350 else _vw_raw,
        })
    blocked_rows = []
    critical_margin_rows = []  # products ALREADY below their margin floor (any recommendation)
    for _, row in df[df["recommendation"] == "LOWER_BLOCKED"].iterrows():
        d = _row_to_dict(row)
        cm  = _safe_float(row.get("current_margin"))
        mf  = _safe_float(row.get("margin_floor"))
        d["margin_gap"] = round((cm - mf) * 100, 1) if cm is not None and mf is not None else None
        d["margin_below_floor"] = bool(row.get("margin_below_floor", False))
        blocked_rows.append(d)

    # Surface products already operating below their margin floor (across all recommendations)
    for _, row in df.iterrows():
        if bool(row.get("margin_below_floor", False)):
            cm = _safe_float(row.get("current_margin"))
            mf = _safe_float(row.get("margin_floor"))
            # Compute price needed to exactly reach the brand margin floor:
            # floor_price = (cogs + fba_fee) / (1 - margin_floor - referral_rate)
            _cogs     = _safe_float(row.get("cogs"))
            _fba      = _safe_float(row.get("avg_fba_fee_per_unit")) or 0.0
            _ref_rate = _safe_float(row.get("avg_referral_rate")) or 0.15
            _denom    = 1.0 - (mf or 0.0) - _ref_rate
            floor_price_val = round((_cogs + _fba) / _denom, 2) if (_cogs is not None and _denom > 0) else None
            _row_d = _row_to_dict(row)
            _raw_reasoning = str(row.get("reasoning", "") or "")
            critical_margin_rows.append({
                "asin":               row.get("asin"),
                "title_short":        _row_d.get("title_short"),
                "size":               _row_d.get("size"),
                "brand":              row.get("brand"),
                "sub_brand":          _row_d.get("sub_brand"),
                "recommendation":     row.get("recommendation"),
                "current_price":      _safe_float(row.get("current_price")),
                "current_margin":     cm,
                "contribution_margin": _safe_float(row.get("contribution_margin")),
                "margin_floor":       mf,
                "margin_gap_pp":      round((cm - mf) * 100, 1) if cm is not None and mf is not None else None,
                "floor_price":        floor_price_val,
                "reasoning":          _raw_reasoning,
                "reasoning_short":    _raw_reasoning[:350].rstrip() + "\u2026" if len(_raw_reasoning) > 350 else _raw_reasoning,
                "price_source":       row.get("price_source"),
                "cogs_source":        row.get("cogs_source"),
            })

    revert_rows = []
    if history_df is not None and not history_df.empty:
        flag_col = history_df.get("revert_flag", pd.Series(False, index=history_df.index))
        revert_df = history_df[flag_col.astype(str).str.lower().isin(["true", "1"])]
        for _, row in revert_df.iterrows():
            _rv_title = str(row.get("title", "") or "")
            # actual_action_taken: what the team actually did (from Pipeline Actions form)
            _actual = str(row.get("actual_action_taken", "") or "").strip()
            _actual_price = _safe_float(row.get("actual_price_implemented"))
            revert_rows.append({
                "asin":                    row.get("asin"),
                "brand":                   row.get("brand"),
                "title_short":             _rv_title[:60].strip() or None,
                "recommendation":          row.get("recommendation"),
                "implemented_date":        str(row.get("implemented_date", ""))[:10],
                "actual_action_taken":     _actual or None,
                "actual_price_implemented":_actual_price,
                "pre_conversion":          _safe_float(row.get("pre_conversion")),
                "post_conversion":         _safe_float(row.get("post_conversion")),
                "pre_net_profit_weekly":   _safe_float(row.get("pre_net_profit_weekly")),
                "post_net_profit_weekly":  _safe_float(row.get("post_net_profit_weekly")),
                "outcome":                 row.get("outcome", "underperforming"),
            })

    # ── Inject 1-click Dismiss / Pipeline URLs into every row ─────────────────
    _dismiss_base = (config or {}).get("email", {}).get("dismiss_script_url", "")

    def _add_action_urls(rows: list) -> None:
        """Mutate each row dict to add dismiss_url and pipeline_url.

        recipient_name is captured from the enclosing scope and embedded
        in the URL as the ``by`` parameter so the Apps Script can log
        who clicked (e.g. "by Shashank").
        """
        for r in rows:
            _a  = r.get("asin", "")
            _b  = r.get("brand", "")
            _t  = r.get("title_short") or r.get("title", "")
            _rc = r.get("recommendation", "")
            r["dismiss_url"]  = _action_url(_dismiss_base, "dismiss",  _a, _b, _t, _rc, by=recipient_name)
            r["pipeline_url"] = _action_url(_dismiss_base, "pipeline", _a, _b, _t, _rc, by=recipient_name)

    _add_action_urls(raise_rows)
    _add_action_urls(lower_rows)
    _add_action_urls(lower_velocity_rows)
    _add_action_urls(blocked_rows)
    _add_action_urls(critical_margin_rows)
    _add_action_urls(revert_rows)

    # ── Bulk "Approve All" URL — one click logs every top-5 RAISE at once ─────
    approve_all_url = _action_url_all(_dismiss_base, raise_rows, by=recipient_name)

    # ── Plain-English signal summary + inline risk warnings (main action rows only) ──
    for r in raise_rows + lower_rows:
        r["signal_summary"] = _build_signal_summary(r)
        r["risk_inline"]    = _build_risk_inline(r)

    # ── Align exec_summary top_3_actions with visible detail rows ─────────────
    # Guarantees every ASIN in the summary appears in the RAISE or LOWER table.
    # Also filters out negative-impact LOWERs so "+$-1090/wk" never appears as a
    # "Top Action" — only genuinely profitable recommendations are surfaced to Neil.
    if exec_summary and exec_summary.get("top_3_actions"):
        # Preserve LLM rationale text keyed by ASIN
        llm_rationale = {
            a["asin"]: a.get("rationale", "")
            for a in exec_summary["top_3_actions"]
            if isinstance(a, dict) and a.get("asin")
        }
        candidates = []
        for r in raise_rows:
            impact = r.get("raise_5pct_weekly_impact") or 0.0
            if impact <= 0:
                continue
            new_price = r.get("raise_5pct_new_price")
            action_str = f"Raise to ${new_price:.2f} (+5%)" if new_price else "Raise price"
            candidates.append({
                "asin":              r["asin"],
                "brand":             r.get("brand"),
                "title_short":       r.get("title_short"),
                "size":              r.get("size"),
                "current_price":     r.get("current_price"),
                "action":            action_str,
                "weekly_impact_usd": round(impact, 0),
                "rationale":         llm_rationale.get(r["asin"], r.get("reasoning", "")),
                "_impact":           impact,
            })
        for r in lower_rows:
            impact = r.get("lower_5pct_scenario_b") or 0.0
            if impact <= 0:
                # Do NOT surface all-negative LOWERs as "top actions" — they're losses
                continue
            new_price = r.get("lower_5pct_new_price")
            action_str = f"Lower to ${new_price:.2f} (-5%)" if new_price else "Lower price"
            candidates.append({
                "asin":              r["asin"],
                "brand":             r.get("brand"),
                "title_short":       r.get("title_short"),
                "size":              r.get("size"),
                "current_price":     r.get("current_price"),
                "action":            action_str,
                "weekly_impact_usd": round(impact, 0),
                "rationale":         llm_rationale.get(r["asin"], r.get("reasoning", "")),
                "_impact":           impact,
            })
        candidates.sort(key=lambda x: x["_impact"], reverse=True)
        top3 = [{k: v for k, v in c.items() if k != "_impact"} for c in candidates[:3]]
        exec_summary["top_3_actions"] = top3  # empty list is fine — template guards on it
        exec_summary["velocity_watch_rows"] = _velocity_watch_rows
    elif exec_summary:
        exec_summary["velocity_watch_rows"] = _velocity_watch_rows

    # ── Headline: top-5 dollar impact for the "Quick Win" banner ─────────────
    # Deliberately uses only the 5 rows shown in the email — this is what the
    # team is being asked to approve, not the whole portfolio opportunity.
    headline_raise_impact = round(sum(
        (_safe_float(r.get("raise_5pct_weekly_impact")) or 0.0)
        for r in raise_rows
        if (_safe_float(r.get("raise_5pct_weekly_impact")) or 0.0) > 0
    ), 0)
    headline_lower_impact = round(sum(
        (_safe_float(r.get("lower_5pct_scenario_b")) or 0.0)
        for r in lower_rows
        if (_safe_float(r.get("lower_5pct_scenario_b")) or 0.0) > 0
    ), 0)

    # ── Performance tracking: ASINs actioned in the last 2–4 weeks ───────────
    performance_rows = _build_performance_rows(history_df, recommendations_df, anchor_date)

    # Bug 9 fix: pre-compute total recoverable margin in Python, not the LLM.
    # LLM arithmetic on 59+ rows is unreliable — this number goes to Neil in the
    # subject line and headline. Pass to LLM as a stated fact via exec_summary.
    # E3 fix: sum ALL RAISE ASINs from the full DataFrame, not just the top-5
    # shown in raise_rows — email caps display at 5 but opportunity is portfolio-wide.
    _raise_opportunity = sum(
        (_safe_float(r_row.get("raise_5pct_weekly_impact")) or 0.0)
        for _, r_row in df[df["recommendation"] == "RAISE"].iterrows()
        if (_safe_float(r_row.get("raise_5pct_weekly_impact")) or 0.0) > 0
    )
    _lower_opportunity = sum(
        (_safe_float(r.get("lower_5pct_scenario_b")) or 0.0)
        for _, r_row in df[df["recommendation"] == "LOWER"].iterrows()
        for r in [_row_to_dict(r_row)]
        if (_safe_float(r.get("lower_5pct_scenario_b")) or 0.0) > 0
    )
    total_opportunity_usd = round(_raise_opportunity + _lower_opportunity, 0)
    if exec_summary is not None:
        exec_summary["total_opportunity_usd"] = total_opportunity_usd

    seasonality = _build_seasonality_section(df, config)

    return {
        "run_date":               run_date,
        "anchor_date":            anchor_date,
        "counts":                 counts,
        "wow_delta":              wow_delta,          # WoW: prev week counts for delta display
        "review_time_mins":       review_time_mins,   # estimated read time for Neil
        "n_actions":              n_actions,          # actionable count (RAISE + profitable LOWER)
        # Top-5 only impacts for the "Quick Win" headline banner
        "headline_raise_impact":  headline_raise_impact,
        "headline_lower_impact":  headline_lower_impact,
        "raise_rows":             raise_rows,
        "lower_rows":             lower_rows,
        "lower_velocity_rows":    lower_velocity_rows,
        "lower_velocity_total":   lower_velocity_total,
        "lower_velocity_shown":   lower_velocity_shown,
        "lower_no_data_count":    lower_no_data_count,
        "blocked_rows":           blocked_rows,
        "revert_rows":            revert_rows,
        "critical_margin_rows":   critical_margin_rows,
        "exec_summary":           exec_summary,
        "dqr":                    data_quality_report,
        "total_opportunity_usd":  total_opportunity_usd,
        "seasonality":            seasonality,
        # Performance tracking block: actioned ASINs from last 2-4 weeks
        "performance_rows":       performance_rows,
        # Bulk approve URL — one click logs all top-5 RAISE rows at once
        "approve_all_url":        approve_all_url,
        # Who else got this email (display names, for footer). Empty when
        # recipient_name is blank (e.g. base_context used for subject/counts).
        "also_sent_to":           also_sent_to or [],
        "recipient_name":         recipient_name,
    }


def render_email_html(context: dict) -> str:
    if not JINJA2_AVAILABLE:
        raise ImportError("jinja2 is required for email rendering")
    env = _load_template_env()
    return env.get_template(TEMPLATE_FILE).render(**context)


# ─────────────────────────────────────────────────────────────────────────────
# Gmail API send
# ─────────────────────────────────────────────────────────────────────────────

def _build_raw_message(sender: str, recipients: list[str], subject: str,
                       plain_text: str, html_body: str,
                       reply_to: Optional[list] = None) -> str:
    """Build a base64url-encoded RFC 2822 message for the Gmail API.

    `reply_to` — if provided, sets the Reply-To header to all those addresses
    so Reply-All from any personalised copy reaches every team member.
    """
    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"]    = sender
    msg["To"]      = ", ".join(recipients)
    if reply_to:
        msg["Reply-To"] = ", ".join(reply_to)
    msg.attach(MIMEText(plain_text, "plain"))
    msg.attach(MIMEText(html_body, "html"))
    raw = base64.urlsafe_b64encode(msg.as_bytes()).decode("utf-8")
    return raw


def send_weekly_email(
    recommendations_df: pd.DataFrame,
    history_df: Optional[pd.DataFrame],
    config: dict,
    run_date: str,
    anchor_date: str,
    exec_summary: Optional[dict] = None,
    data_quality_report: Optional[dict] = None,
) -> None:
    """Send the weekly pricing alert email via Gmail API (OAuth 2.0).

    No-op with warning if gmail_token.json is missing (run authorize_gmail.py first).
    """
    if not GMAIL_API_AVAILABLE:
        logger.warning("google-api-python-client not installed — skipping email send")
        return
    if not JINJA2_AVAILABLE:
        logger.warning("jinja2 not installed — skipping email send")
        return

    sender     = config["email"].get("sender", "")
    recipients = config["email"].get("recipients", [])

    if not sender:
        logger.warning("email.sender not set in config.yaml — skipping email send")
        return
    if not recipients:
        logger.warning("email.recipients empty in config.yaml — skipping email send")
        return

    # Load OAuth credentials (auto-refresh if expired)
    creds = _load_gmail_credentials(config)
    if creds is None:
        return  # warning already logged inside _load_gmail_credentials

    # Build a baseline context (no recipient name) to extract counts + subject.
    # Then re-render per-recipient with their name embedded in action URLs.
    try:
        base_context = build_email_context(
            recommendations_df, history_df, run_date, anchor_date,
            exec_summary=exec_summary, config=config,
            data_quality_report=data_quality_report,
            recipient_name="",          # no name → by= param omitted
        )
    except Exception as exc:
        logger.error("Failed to build email context: %s", exc)
        return

    counts    = base_context["counts"]
    n_actions = base_context.get("n_actions", counts["RAISE"] + counts["LOWER"])

    # Subject line tier — tells Neil at a glance how much attention this week needs
    if n_actions >= 15:
        tier = "🔴 Action needed"
    elif n_actions >= 5:
        tier = "🟡 Normal week"
    else:
        tier = "🟢 Light week"
    subject = f"Six10 Weekly Pricing Alert — {tier} ({n_actions} price actions) — {run_date}"

    n_lower_vd = counts.get("LOWER_VELOCITY_DEFENCE", 0)
    plain_text = (
        f"Six10 Weekly Pricing Alert - {run_date}\n"
        f"Anchor date: {anchor_date}\n\n"
        f"Summary: {counts['RAISE']} RAISE | {counts['LOWER']} LOWER | "
        f"{n_lower_vd} LOWER_VD | {counts['LOWER_BLOCKED']} BLOCKED | {counts['HOLD']} HOLD\n"
        f"Total ASINs: {counts['total']}\n\n"
        "See HTML version for full details.\n"
    )

    # ── Send one personalised email per recipient ─────────────────────────────
    # Each recipient gets action URLs with their name pre-filled (&by=Shashank)
    # so the Apps Script can log who clicked Dismiss / Actioned in the Sheet.
    #
    # Reply-To is set to ALL recipients on every copy so Reply-All works
    # correctly even though each email is individually addressed.
    #
    # "Also sent to: X, Y" footer tells each person who else is on the list.
    service = build("gmail", "v1", credentials=creds)
    sent_count = 0
    for recipient_email in recipients:
        try:
            name       = _name_from_email(recipient_email)
            # Names of everyone ELSE on the list — shown in footer
            others     = [_name_from_email(e) for e in recipients if e != recipient_email]

            personalised_context = build_email_context(
                recommendations_df, history_df, run_date, anchor_date,
                exec_summary=exec_summary, config=config,
                data_quality_report=data_quality_report,
                recipient_name=name,
                also_sent_to=others,
            )
            html_body = render_email_html(personalised_context)
            raw = _build_raw_message(
                sender, [recipient_email], subject, plain_text, html_body,
                reply_to=recipients,          # Reply-All reaches every team member
            )
            service.users().messages().send(userId="me", body={"raw": raw}).execute()
            sent_count += 1
            logger.info("Email sent to %s (by=%s, also_sent_to=%s)", recipient_email, name, others)
        except HttpError as exc:
            logger.error("Gmail API error sending email to %s: %s", recipient_email, exc)
        except Exception as exc:
            logger.error("Failed to render/send email to %s: %s", recipient_email, exc)

    logger.info(
        "Weekly email dispatched to %d/%d recipient(s): "
        "%d RAISE | %d LOWER | %d BLOCKED | %d HOLD",
        sent_count, len(recipients),
        counts["RAISE"], counts["LOWER"],
        counts["LOWER_BLOCKED"], counts["HOLD"],
    )
