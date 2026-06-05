"""
llm_layer.py -- LLM enrichment layer for the Six10 Pricing Strategy Engine.

Architecture: Cache-Augmented Generation (CAG) with Anthropic prompt caching.
  - System prompt (cached): business rules + COGS sheet + product families
  - User turn (fresh each run): active recommendations as compact JSON
  - Single API call per run -- all ASINs processed together for portfolio context

Fallback guarantee: enrich() NEVER raises. Any failure returns the original
recommendations_df unchanged with exec_summary=None.

LLM never modifies: recommendation, confidence, or any numeric column.
LLM replaces: the `reasoning` column with business-narrative text.
LLM adds: exec_summary dict consumed by email_reporter.py.

Windows-safe: all logger strings use ASCII only (no Unicode arrows or emoji).
"""

import concurrent.futures
import json
import logging
import os
from typing import Optional

import pandas as pd

logger = logging.getLogger(__name__)

try:
    import anthropic
    ANTHROPIC_AVAILABLE = True
except ImportError:
    ANTHROPIC_AVAILABLE = False
    logger.warning("anthropic SDK not installed -- LLM layer disabled")

try:
    from dotenv import load_dotenv
    load_dotenv(override=True)
except ImportError:
    pass

# Fields serialized per ASIN into the user turn.
# Chosen for reasoning relevance; excludes margin-calculator outputs (not yet
# computed at Step 4.5) and pre-change baselines (not needed for narrative).
_ASIN_FIELDS = [
    "asin", "brand", "product_category", "product_family",
    "cogs_name",          # human-readable product name WITH size (e.g. "Spa Defoamer - 1 Pint")
    "recommendation", "confidence",
    "current_price", "cogs", "cogs_source",
    "current_margin", "margin_floor",
    "avg_weekly_units",
    "composite_score",
    "ctr_score", "conversion_score", "velocity_score",
    "margin_score", "refund_score",
    "avg_ctr", "brand_avg_ctr",
    "avg_conversion", "baseline_conversion",
    "avg_refund_rate",
    "days_of_supply",
    "margin_below_floor",
    "price_source",
    # Margin calculator outputs (available after Step 5)
    "raise_5pct_new_price", "raise_5pct_weekly_impact",
    "raise_10pct_new_price", "raise_10pct_weekly_impact",
    "lower_5pct_scenario_b",   # optimistic (with demand uplift) weekly impact
    "lower_5pct_viable",
    "lower_10pct_viable",
]

# LLM output JSON schema shown to the model in the system prompt.
_OUTPUT_SCHEMA = """{
  "exec_summary": {
    "total_opportunity_usd": <number | null>,
    "gross_margin_gap_analysis": "<1-2 sentence portfolio view for the CEO>",
    "top_3_actions": [
      {
        "asin": "<B0...>",
        "brand": "<brand>",
        "action": "<verb + target price, e.g. Raise to $57.99>",
        "rationale": "<one sentence business case>",
        "weekly_impact_usd": <estimated number | null>
      }
    ],
    "critical_flags": ["<short actionable flag string>", ...],
    "cogs_gap_summary": "<X products on HOLD -- COGS missing. Est. $Y/wk opportunity locked.>"
  },
  "recommendations": {
    "<ASIN>": {
      "reasoning": "<1-2 sentence business narrative, no jargon, readable by a non-technical executive>",
      "anomaly_flags": ["<short specific flag>", ...]
    }
  }
}"""


class LLMLayer:
    """
    Wraps Anthropic Claude API calls with prompt caching for Six10 enrichment.

    Usage in main.py Step 4.5:
        layer = LLMLayer(config, cogs_df, families_df)
        recommendations_df, exec_summary = layer.enrich(recommendations_df)
    """

    def __init__(self, config: dict, cogs_df: pd.DataFrame, families_df: pd.DataFrame):
        self.config = config
        self.cogs_df = cogs_df
        self.families_df = families_df

        llm_cfg = config.get("llm", {})
        self.model = llm_cfg.get("model", "claude-sonnet-4-6")
        self.max_tokens = llm_cfg.get("max_tokens", 8192)
        self.timeout = llm_cfg.get("timeout_seconds", 90)
        config_enabled = llm_cfg.get("enabled", True)

        api_key = os.environ.get("ANTHROPIC_API_KEY", "").strip()

        if not ANTHROPIC_AVAILABLE:
            logger.warning("anthropic SDK not installed -- LLM layer disabled")
            self.enabled = False
            self._client = None
            return

        if not api_key:
            logger.warning("ANTHROPIC_API_KEY not set -- LLM layer disabled")
            self.enabled = False
            self._client = None
            return

        if not config_enabled:
            logger.info("LLM layer disabled in config (llm.enabled: false)")
            self.enabled = False
            self._client = None
            return

        self.enabled = True
        self._client = anthropic.Anthropic(api_key=api_key)
        logger.info("LLM layer initialised (model=%s, max_tokens=%d)", self.model, self.max_tokens)

    # ------------------------------------------------------------------
    # System prompt construction (cached portion)
    # ------------------------------------------------------------------

    def _serialize_cogs(self) -> str:
        records = []
        for _, row in self.cogs_df.iterrows():
            cogs_val = row.get("cogs")
            records.append({
                "asin": str(row.get("asin", "")).upper().strip(),
                "brand": str(row.get("brand_key", row.get("brand", ""))).strip(),
                "cogs": round(float(cogs_val), 2) if pd.notna(cogs_val) else None,
            })
        return json.dumps(records, sort_keys=True, separators=(",", ":"))

    def _serialize_families(self) -> str:
        records = []
        for _, row in self.families_df.iterrows():
            records.append({
                "asin": str(row.get("asin", "")).upper().strip(),
                "brand": str(row.get("brand", "")).strip(),
                "category": str(row.get("product_category", "") or ""),
                "family": str(row.get("product_family", "") or ""),
            })
        return json.dumps(records, sort_keys=True, separators=(",", ":"))

    def _serialize_config_rules(self) -> str:
        brands = {}
        for brand, cfg in self.config.get("brands", {}).items():
            brands[brand] = {
                "margin_floor_pct": round(cfg.get("margin_floor", 0) * 100, 1),
                "price_elasticity": cfg.get("price_elasticity"),
                "seasonal": cfg.get("seasonal", False),
            }
        rules = {
            "brands": brands,
            "signal_weights": self.config.get("signals", {}).get("weights", {}),
            "decision_thresholds": {
                "raise_composite_score": self.config.get("signals", {}).get("thresholds", {}).get("raise_score", 0.25),
                "lower_composite_score": self.config.get("signals", {}).get("thresholds", {}).get("lower_score", -0.25),
            },
            "confidence_config": self.config.get("confidence", {}),
        }
        return json.dumps(rules, sort_keys=True, separators=(",", ":"))

    def _build_system_prompt(self) -> str:
        return f"""You are a senior pricing analyst for Six10 Ventures, an Amazon seller managing 5+ brands and approximately 187 ASINs across pool care, health supplements, pet care, and eye health categories.

Your outputs are read by:
- Neil (CEO): reads only the executive summary. Wants dollar amounts, gross margin bridge language, and 3 clear actions.
- Amber (Director, he/him): reads per-product reasoning. Wants plain-English explanations and clear distinction between pricing issues vs return-rate issues.

BUSINESS RULES:
{self._serialize_config_rules()}

COGS REFERENCE TABLE (use to detect outliers -- if a product's COGS is far outside its brand peers, flag it):
{self._serialize_cogs()}

PRODUCT FAMILIES (use for cross-ASIN pattern analysis):
{self._serialize_families()}

YOUR TASKS (given the recommendations JSON in the user message):
1. Write business-narrative reasoning for every actionable ASIN (RAISE/LOWER/LOWER_BLOCKED).
2. Generate a CEO executive summary:
   - total_opportunity_usd: USE THE VALUE PROVIDED IN total_opportunity_usd field of the input payload. Do NOT compute it yourself -- it has been pre-calculated in Python to ensure accuracy.
   - gross_margin_gap_analysis: 1-2 sentence portfolio view.
   - top_3_actions: pick the 3 highest-impact ASINs (by raise_5pct_weekly_impact or lower_5pct_scenario_b), populate weekly_impact_usd from those columns.
   - critical_flags: anomalies, refund risks, COGS issues.
   - cogs_gap_summary: use hold_summary.hold_cogs_missing_count and hold_summary.hold_cogs_missing_asins from the input.
3. Flag anomalies per ASIN: COGS outliers vs brand peers, RAISE on high-refund products (>40%), price source quality issues, products at extreme negative margin.
4. COGS gaps are already summarised in hold_summary -- use those counts directly in the exec_summary.

REASONING GUIDELINES (CRITICAL -- output token budget is tight):
- Maximum 2 sentences, maximum 350 characters total. Be specific and business-readable. Count characters before submitting.
- Never use internal jargon: no "composite_score", "signal score", "CTR signal".
- Do say: "strong search demand", "conversion lagging", "healthy margin buffer", "refund rate is the core issue".
- Always mention the margin situation if margin_below_floor is true.
- For LOWER: mention the volume recovery angle in one sentence.
- For LOWER_BLOCKED: say why the cut is blocked (margin floor) in one sentence.
- For HOLD: one sentence on what is mixed or missing.

PRODUCT DISPLAY NAME IN FLAGS:
- Use cogs_name as the product label in critical_flags and anomaly_flags. It includes the size variant (e.g. "Spa Defoamer - 1 Pint", "Pool Clarifier 32oz").
- If cogs_name is null or missing, fall back to product_family.
- Always include the size if it appears in cogs_name. Example: "B08QNH9QCT (Spa Defoamer - 1 Pint): ..." not "B08QNH9QCT (Spa Defoamer): ..."

ANOMALY FLAGS (keep short, specific, actionable):
- "COGS may be a data entry error -- $X is far above brand average"
- "RAISE on product with XX% refund rate -- verify returns before acting"
- "Price from inventory snapshot, not live sales -- treat recommendation with lower confidence"
- "Margin at -XX% -- may be a clearance listing or incorrect COGS"

OUTPUT FORMAT (return ONLY this JSON, no preamble, no markdown fences outside the object):
{_OUTPUT_SCHEMA}

HARD CONSTRAINTS:
- Do NOT change the recommendation value (RAISE/LOWER/HOLD/LOWER_BLOCKED).
- Do NOT change confidence level.
- Do NOT invent numeric values -- use null if unknown.
- Do NOT hallucinate ASIN data not present in the input.
- weekly_impact_usd in exec_summary.top_3_actions: estimate using current_price * avg_weekly_units * projected_margin_change -- use null if you cannot estimate.
- total_opportunity_usd in exec_summary: copy the value from the input payload's total_opportunity_usd field -- do NOT compute it yourself.
- Every ASIN in the input must appear in recommendations output.
"""

    # ------------------------------------------------------------------
    # User turn construction (fresh each run)
    # ------------------------------------------------------------------

    def _build_user_turn(self, recommendations_df: pd.DataFrame) -> str:
        # Only send actionable ASINs (RAISE/LOWER/LOWER_BLOCKED) for per-product
        # reasoning — these are the ones that appear in the email detail tables.
        # HOLD ASINs are summarised as aggregate stats to keep payload small.
        actionable_mask = recommendations_df["recommendation"].isin(
            ["RAISE", "LOWER", "LOWER_BLOCKED"]
        )
        actionable_df = recommendations_df[actionable_mask]
        hold_df = recommendations_df[~actionable_mask]

        asins = []
        for _, row in actionable_df.iterrows():
            record = {}
            for field in _ASIN_FIELDS:
                val = row.get(field)
                if isinstance(val, float) and pd.isna(val):
                    val = None
                elif isinstance(val, pd.Timestamp):
                    val = str(val)[:10]
                elif hasattr(val, "item"):
                    val = val.item()
                record[field] = val
            asins.append(record)

        # HOLD summary for COGS gap detection and exec summary context
        hold_cogs_missing = hold_df[
            hold_df.get("cogs_source", pd.Series(dtype=str)).fillna("missing") == "missing"
        ] if not hold_df.empty and "cogs_source" in hold_df.columns else pd.DataFrame()

        hold_summary = {
            "total_hold_count": int(len(hold_df)),
            "hold_cogs_missing_count": int(len(hold_cogs_missing)),
            "hold_cogs_missing_asins": list(hold_cogs_missing.get("asin", pd.Series()).astype(str).head(20)),
        }

        # Bug 9 fix: pre-compute total_opportunity_usd in Python so the LLM
        # copies a verified number rather than summing 59+ rows itself.
        _raise_opp = sum(
            (r.get("raise_5pct_weekly_impact") or 0.0)
            for _, r in actionable_df[actionable_df["recommendation"] == "RAISE"].iterrows()
            if (r.get("raise_5pct_weekly_impact") or 0.0) > 0
        )
        _lower_opp = sum(
            (r.get("lower_5pct_scenario_b") or 0.0)
            for _, r in actionable_df[actionable_df["recommendation"] == "LOWER"].iterrows()
            if (r.get("lower_5pct_scenario_b") or 0.0) > 0
        )
        total_opportunity_usd = round(_raise_opp + _lower_opp, 0)

        payload = {
            "task": (
                "Analyse these Amazon pricing recommendations. "
                "Write narrative reasoning for each actionable ASIN (RAISE/LOWER/LOWER_BLOCKED). "
                "Produce the CEO executive summary using the actionable recs + hold_summary context. "
                "Flag anomalies per ASIN. "
                "Return only the JSON object."
            ),
            "total_opportunity_usd": total_opportunity_usd,
            "actionable_count": len(asins),
            "hold_summary": hold_summary,
            "recommendations": asins,
        }
        return json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)

    # ------------------------------------------------------------------
    # API call with timeout + caching
    # ------------------------------------------------------------------

    def _call_api(self, system_prompt: str, user_turn: str):
        def _make_call():
            return self._client.messages.create(
                model=self.model,
                max_tokens=self.max_tokens,
                system=[
                    {
                        "type": "text",
                        "text": system_prompt,
                        "cache_control": {"type": "ephemeral"},
                    }
                ],
                messages=[{"role": "user", "content": user_turn}],
            )

        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:
            future = executor.submit(_make_call)
            try:
                response = future.result(timeout=self.timeout)
            except concurrent.futures.TimeoutError:
                logger.warning(
                    "LLM API call timed out after %ds -- using template reasoning",
                    self.timeout,
                )
                return None

        usage = response.usage
        cache_read = getattr(usage, "cache_read_input_tokens", 0)
        cache_write = getattr(usage, "cache_creation_input_tokens", 0)
        logger.info(
            "LLM API call complete -- cache_read=%d cache_write=%d input=%d output=%d stop=%s",
            cache_read, cache_write, usage.input_tokens, usage.output_tokens,
            response.stop_reason,
        )
        if response.stop_reason == "max_tokens":
            logger.warning(
                "LLM output hit max_tokens limit (%d) -- JSON may be truncated. "
                "Increase llm.max_tokens in config.yaml or reduce ASIN count.",
                self.max_tokens,
            )
        return response

    # ------------------------------------------------------------------
    # Response parsing
    # ------------------------------------------------------------------

    def _parse_llm_response(self, raw_text: str) -> Optional[dict]:
        text = raw_text.strip()

        # Strip markdown code fences if present
        if text.startswith("```"):
            parts = text.split("```")
            for part in parts:
                stripped = part.strip()
                if stripped.startswith("json"):
                    stripped = stripped[4:].strip()
                if stripped.startswith("{"):
                    text = stripped
                    break

        # Find first { in case there is conversational preamble
        idx = text.find("{")
        if idx > 0:
            text = text[idx:]

        try:
            data = json.loads(text)
        except json.JSONDecodeError as exc:
            logger.warning("LLM response JSON parse failed: %s", str(exc)[:120])
            return None

        if "exec_summary" not in data or "recommendations" not in data:
            logger.warning("LLM response missing required keys -- got keys: %s", list(data.keys()))
            return None

        return data

    # ------------------------------------------------------------------
    # Apply reasoning back to DataFrame
    # ------------------------------------------------------------------

    def _apply_reasoning(
        self, recommendations_df: pd.DataFrame, llm_output: dict
    ) -> pd.DataFrame:
        df = recommendations_df.copy()
        recs = llm_output.get("recommendations", {})

        replaced = 0
        for idx, row in df.iterrows():
            asin = str(row.get("asin", "")).strip().upper()
            if asin in recs:
                narrative = str(recs[asin].get("reasoning", "")).strip()
                if narrative:
                    df.at[idx, "reasoning"] = narrative
                    replaced += 1

        logger.info("LLM reasoning applied to %d / %d ASINs", replaced, len(df))
        return df

    # ------------------------------------------------------------------
    # Public entry point
    # ------------------------------------------------------------------

    def enrich(
        self, recommendations_df: pd.DataFrame
    ) -> tuple[pd.DataFrame, Optional[dict]]:
        """
        Main entry point called from main.py Step 4.5.

        Returns:
            (enriched_df, exec_summary_dict) on success
            (original_df, None) on any failure -- NEVER raises
        """
        if not self.enabled:
            logger.info("LLM layer disabled -- skipping enrichment")
            return recommendations_df, None

        if recommendations_df.empty:
            logger.info("LLM layer: empty recommendations -- skipping")
            return recommendations_df, None

        try:
            import time
            t0 = time.monotonic()

            system_prompt = self._build_system_prompt()
            user_turn = self._build_user_turn(recommendations_df)

            logger.info(
                "LLM prompt sizes -- system ~%d chars, user ~%d chars",
                len(system_prompt), len(user_turn),
            )

            response = self._call_api(system_prompt, user_turn)
            if response is None:
                return recommendations_df, None

            raw_text = next(
                (block.text for block in response.content if block.type == "text"), ""
            )
            llm_output = self._parse_llm_response(raw_text)
            if llm_output is None:
                return recommendations_df, None

            enriched_df = self._apply_reasoning(recommendations_df, llm_output)
            exec_summary = llm_output.get("exec_summary") or None

            elapsed = time.monotonic() - t0
            logger.info("LLM enrichment complete in %.1fs", elapsed)

            return enriched_df, exec_summary

        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "LLM enrichment failed unexpectedly -- using template reasoning: %s",
                str(exc)[:300],
            )
            return recommendations_df, None
