"""pdf_intelligence — PDF v2 cosmic-grade upgrade pack.

Five investor-grade features bolted on top of vadim_pdf.py:
  1. investment_thesis     — Cerebras/Groq-driven 4-6 sentence thesis
  2. chart_annotations     — trend / anomaly / median-line overlays
  3. price_forecast        — 12/24/36 mo LinReg + CI forecast
  4. liquidity_tier        — A/B/C/D tier based on DLD velocity
  5. photo_analyzer        — Gemini Vision photo analysis

All five are optional — each wrapped try/except inside vadim_pdf.py so a failure
of any single feature never breaks PDF generation.

Free-tier only: Cerebras → Groq → Anthropic chain via llm_chain; Gemini Flash
for vision. NO paid services.
"""
from __future__ import annotations

__version__ = "1.0.0"

# Public API — re-exported for `from pdf_intelligence import ...`
from .investment_thesis import generate_thesis, fallback_thesis  # noqa: F401
from .chart_annotations import annotate_trend_chart  # noqa: F401
from .price_forecast import forecast_prices, build_forecast_chart  # noqa: F401
from .liquidity_tier import compute_liquidity_tier, tier_badge_color  # noqa: F401
from .photo_analyzer import analyze_property_photos, format_photo_analysis  # noqa: F401
