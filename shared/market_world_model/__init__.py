"""Layer 13 — Dubai Market World Model.

Unified market intelligence layer: every entity (area, building, property type,
developer) is modeled as a node with state (price, supply, demand, liquidity,
volatility, sentiment), wired into a relationship graph, and forecast by a
per-(entity, metric) Prophet model.

Public surface (designed to be called by every bot):

    from shared.market_world_model import api as market

    market.ask("Что будет с Marina через 6 месяцев?")
    market.forecast(entity="Dubai Marina", metric="price_per_sqft", horizon_months=6)
    market.what_if(entity="Business Bay", scenario="supply +30%")
    market.compare("Dubai Marina", "JBR")
"""

from . import api  # re-export
from .api import DubaiMarketModel, ask, forecast, what_if, compare  # noqa: F401

__all__ = ["api", "DubaiMarketModel", "ask", "forecast", "what_if", "compare"]
