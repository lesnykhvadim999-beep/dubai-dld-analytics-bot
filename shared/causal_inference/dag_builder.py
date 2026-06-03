"""dag_builder.py — produces causal DAGs (GML strings) consumable by DoWhy.

For each prebuilt study we encode a domain DAG: confounders feed both
the treatment and the outcome; the treatment feeds the outcome. Extra
edges (e.g. supply_index → handover_wave) are added where the Dubai
real-estate context demands them.
"""

from __future__ import annotations

from typing import Iterable, List, Tuple


def _gml(nodes: Iterable[str], edges: Iterable[Tuple[str, str]]) -> str:
    """Render a minimal GML graph that DoWhy can parse."""
    node_block = "\n".join(f'  node [ id "{n}" label "{n}" ]' for n in nodes)
    edge_block = "\n".join(
        f'  edge [ source "{s}" target "{t}" ]' for s, t in edges
    )
    return f"graph [\n  directed 1\n{node_block}\n{edge_block}\n]"


def default_dag(treatment: str, outcome: str, confounders: List[str]) -> str:
    """Backdoor DAG: every confounder → T and → Y, plus T → Y."""
    nodes = {treatment, outcome, *confounders}
    edges: List[Tuple[str, str]] = [(treatment, outcome)]
    for c in confounders:
        edges.append((c, treatment))
        edges.append((c, outcome))
    return _gml(nodes, edges)


# Domain-specific DAGs override the generic one when extra edges matter.
def dag_metro_on_price(confounders: List[str]) -> str:
    """metro_opened → price_per_sqft, with demand_score modulating both."""
    treatment, outcome = "metro_opened", "price_per_sqft"
    edges = [(treatment, outcome)]
    for c in confounders:
        edges += [(c, treatment), (c, outcome)]
    # demand drives supply expectations as well
    if "demand_score" in confounders and "supply_index" in confounders:
        edges.append(("demand_score", "supply_index"))
    return _gml({treatment, outcome, *confounders}, set(edges))


def dag_handover_on_yield(confounders: List[str]) -> str:
    treatment, outcome = "handover_wave", "rental_yield"
    edges = [(treatment, outcome)]
    for c in confounders:
        edges += [(c, treatment), (c, outcome)]
    # supply_index already feeds the treatment via the loop above —
    # do not duplicate the edge.
    return _gml({treatment, outcome, *confounders}, set(edges))


def dag_developer_premium(confounders: List[str]) -> str:
    treatment, outcome = "is_top_developer", "price_per_sqft"
    edges = [(treatment, outcome)]
    for c in confounders:
        edges += [(c, treatment), (c, outcome)]
    return _gml({treatment, outcome, *confounders}, edges)


def dag_view_premium(confounders: List[str]) -> str:
    treatment, outcome = "is_sea_view", "price_per_sqft"
    edges = [(treatment, outcome)]
    for c in confounders:
        edges += [(c, treatment), (c, outcome)]
    return _gml({treatment, outcome, *confounders}, edges)


def dag_floor_on_price(confounders: List[str]) -> str:
    treatment, outcome = "floor", "price_per_sqft"
    edges = [(treatment, outcome)]
    for c in confounders:
        edges += [(c, treatment), (c, outcome)]
    return _gml({treatment, outcome, *confounders}, edges)
