"""
Graph Engine — the core intelligence of Lansky.

Pipeline:
  1. Load all transactions from SQLite
  2. Build a fully-connected weighted graph (nodes = tx IDs, edges = similarity)
  3. Run Louvain community detection to find partitions
  4. Label each partition via purity + support gating (TAU_P, N_MIN)
  5. Classify a new transaction by aggregate edge weight to each partition
  6. Explain the classification by decomposing the composite score
"""

from __future__ import annotations

import logging
import math
from collections import Counter
from dataclasses import dataclass, field
from typing import Any

import jellyfish
import networkx as nx
import community as community_louvain  # python-louvain

from src import config
from src.tools import sql_tool

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Data types
# ---------------------------------------------------------------------------

@dataclass
class TxNode:
    id: str
    direction: str
    from_: str
    to: str
    date: str   # YYYY-MM-DD
    time: str   # HH:MM:SS
    amount: int
    currency: str
    has_description: bool
    description: str | None

    @property
    def day_of_month(self) -> int:
        return int(self.date.split("-")[2])

    @property
    def hour_of_day(self) -> float:
        h, m, s = self.time.split(":")
        return int(h) + int(m) / 60.0

    @property
    def merchant(self) -> str:
        return self.to if self.direction == "out" else self.from_


@dataclass
class Partition:
    id: int
    node_ids: list[str]
    label: str | None = None        # dominant description, if partition is trusted
    purity: float = 0.0
    support: int = 0                # count of labeled nodes in this partition


@dataclass
class ClassificationResult:
    tx_id: str
    partition_id: int | None
    label: str | None               # None = unresolved, ask the user
    confidence: float               # aggregate similarity score (0–4 max)
    explanation: str
    dim_scores: dict[str, float] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Similarity functions — all return float in [0.0, 1.0]
# ---------------------------------------------------------------------------

def _gaussian(delta: float, sigma: float) -> float:
    return math.exp(-(delta ** 2) / (2 * sigma ** 2))


def sim_date(d1: int, d2: int) -> float:
    """Gaussian kernel on day-of-month distance (periodic mod 30)."""
    delta = min(abs(d1 - d2), 30 - abs(d1 - d2))
    return _gaussian(delta, config.SIGMA_DATE)


def sim_time(h1: float, h2: float) -> float:
    """Gaussian kernel on hour-of-day distance (periodic mod 24)."""
    delta = min(abs(h1 - h2), 24 - abs(h1 - h2))
    return _gaussian(delta, config.SIGMA_TIME)


def sim_amount(a1: int, a2: int) -> float:
    """Log-scale proximity; returns 0 if either amount is non-positive."""
    if a1 <= 0 or a2 <= 0:
        return 0.0
    return _gaussian(math.log(a1) - math.log(a2), config.SIGMA_AMOUNT)


def sim_merchant(m1: str, m2: str) -> float:
    """Jaro-Winkler similarity on merchant names (case-insensitive)."""
    return jellyfish.jaro_winkler_similarity(m1.upper(), m2.upper())


def composite_similarity(a: TxNode, b: TxNode) -> dict[str, float]:
    """Return all four dimension scores and their sum."""
    sd = sim_date(a.day_of_month, b.day_of_month)
    st = sim_time(a.hour_of_day, b.hour_of_day)
    sa = sim_amount(a.amount, b.amount)
    sm = sim_merchant(a.merchant, b.merchant)
    return {
        "date": sd,
        "time": st,
        "amount": sa,
        "merchant": sm,
        "total": sd + st + sa + sm,
    }


# ---------------------------------------------------------------------------
# Graph construction
# ---------------------------------------------------------------------------

def _load_nodes() -> list[TxNode]:
    rows = sql_tool.fetch_all()
    return [
        TxNode(
            id=r["id"],
            direction=r["direction"],
            from_=r["from"],
            to=r["to"],
            date=r["date"],
            time=r["time"],
            amount=r["amount"],
            currency=r["currency"],
            has_description=bool(r["has_description"]),
            description=r["description"],
        )
        for r in rows
    ]


def build_graph(nodes: list[TxNode]) -> nx.Graph:
    G = nx.Graph()
    for n in nodes:
        G.add_node(n.id)
    for i in range(len(nodes)):
        for j in range(i + 1, len(nodes)):
            scores = composite_similarity(nodes[i], nodes[j])
            G.add_edge(nodes[i].id, nodes[j].id, weight=scores["total"], dims=scores)
    log.info("GRAPH built  nodes=%d  edges=%d", G.number_of_nodes(), G.number_of_edges())
    return G


# ---------------------------------------------------------------------------
# Louvain partitioning
# ---------------------------------------------------------------------------

def detect_partitions(G: nx.Graph, nodes: list[TxNode]) -> list[Partition]:
    if len(G.nodes) == 0:
        return []

    if len(G.nodes) == 1:
        node_id = list(G.nodes)[0]
        return [Partition(id=0, node_ids=[node_id])]

    partition_map: dict[str, int] = community_louvain.best_partition(G, weight="weight")

    # Group node IDs by partition ID
    groups: dict[int, list[str]] = {}
    for node_id, part_id in partition_map.items():
        groups.setdefault(part_id, []).append(node_id)

    # Build a lookup for descriptions
    desc_by_id: dict[str, str | None] = {n.id: n.description for n in nodes}

    partitions = []
    for part_id, node_ids in groups.items():
        described = [desc_by_id[nid] for nid in node_ids if desc_by_id[nid]]
        support = len(described)
        if support > 0:
            counts = Counter(described)
            dominant, dominant_count = counts.most_common(1)[0]
            purity = dominant_count / support
        else:
            dominant, purity = None, 0.0

        labeled = (
            dominant is not None
            and purity >= config.TAU_P
            and support >= config.N_MIN
        )

        partitions.append(Partition(
            id=part_id,
            node_ids=node_ids,
            label=dominant if labeled else None,
            purity=purity,
            support=support,
        ))

    labeled_count = sum(1 for p in partitions if p.label is not None)
    log.info(
        "GRAPH partitions=%d  labeled=%d  unlabeled=%d",
        len(partitions), labeled_count, len(partitions) - labeled_count,
    )
    for p in partitions:
        log.info(
            "  partition=%d  nodes=%d  label=%r  purity=%.0f%%  support=%d",
            p.id, len(p.node_ids), p.label, p.purity * 100, p.support,
        )
    return partitions


# ---------------------------------------------------------------------------
# Classification of a new (uncategorized) transaction
# ---------------------------------------------------------------------------

def classify(
    new_tx: dict[str, Any],
    nodes: list[TxNode] | None = None,
    G: nx.Graph | None = None,
    partitions: list[Partition] | None = None,
) -> ClassificationResult:
    """
    Classify a single uncategorized transaction against the current graph.

    Pass pre-built nodes/G/partitions to avoid redundant DB calls when
    classifying in batch; omit to have the engine load fresh from SQLite.
    """
    if nodes is None:
        nodes = _load_nodes()
    if G is None:
        G = build_graph(nodes)
    if partitions is None:
        partitions = detect_partitions(G, nodes)

    candidate = TxNode(
        id=new_tx["id"],
        direction=new_tx["direction"],
        from_=new_tx["from"],
        to=new_tx["to"],
        date=new_tx["date"],
        time=new_tx["time"],
        amount=new_tx["amount"],
        currency=new_tx["currency"],
        has_description=False,
        description=None,
    )

    existing_nodes = [n for n in nodes if n.id != candidate.id]
    if not existing_nodes or not partitions:
        return ClassificationResult(
            tx_id=candidate.id,
            partition_id=None,
            label=None,
            confidence=0.0,
            explanation="Not enough data to classify yet — I'll ask you.",
        )

    # Map each existing node to its partition
    node_to_partition: dict[str, int] = {
        nid: p.id for p in partitions for nid in p.node_ids
    }

    # Aggregate similarity scores per partition
    partition_scores: dict[int, dict[str, float]] = {}
    for node in existing_nodes:
        pid = node_to_partition.get(node.id)
        if pid is None:
            continue
        scores = composite_similarity(candidate, node)
        if pid not in partition_scores:
            partition_scores[pid] = {"date": 0.0, "time": 0.0, "amount": 0.0, "merchant": 0.0, "total": 0.0, "count": 0.0}
        for dim in ("date", "time", "amount", "merchant", "total"):
            partition_scores[pid][dim] += scores[dim]
        partition_scores[pid]["count"] += 1

    if not partition_scores:
        return ClassificationResult(
            tx_id=candidate.id,
            partition_id=None,
            label=None,
            confidence=0.0,
            explanation="No existing partitions to compare against.",
        )

    # Best partition = highest aggregate total score
    best_pid = max(partition_scores, key=lambda pid: partition_scores[pid]["total"])
    best_scores = partition_scores[best_pid]
    count = best_scores["count"]
    avg_dims = {dim: best_scores[dim] / count for dim in ("date", "time", "amount", "merchant")}
    confidence = best_scores["total"] / count  # avg composite (0–4)

    best_partition = next(p for p in partitions if p.id == best_pid)
    label = best_partition.label  # None if unresolved

    explanation = _explain(candidate, best_partition, avg_dims, label)

    log.info(
        "GRAPH classify  merchant=%r  → partition=%d  label=%r  confidence=%.3f",
        candidate.merchant, best_pid, label, confidence,
    )
    return ClassificationResult(
        tx_id=candidate.id,
        partition_id=best_pid,
        label=label,
        confidence=confidence,
        explanation=explanation,
        dim_scores=avg_dims,
    )


# ---------------------------------------------------------------------------
# Explainability
# ---------------------------------------------------------------------------

def _explain(
    tx: TxNode,
    partition: Partition,
    avg_dims: dict[str, float],
    label: str | None,
) -> str:
    # Rank dimensions by score
    ranked = sorted(avg_dims.items(), key=lambda x: x[1], reverse=True)
    strong = [d for d, s in ranked if s >= 0.6]
    weak   = [d for d, s in ranked if s < 0.3]

    dim_labels = {
        "date": f"day-of-month ({tx.day_of_month})",
        "time": f"time-of-day ({tx.time})",
        "amount": f"amount ({tx.amount:,} {tx.currency})",
        "merchant": f"merchant ('{tx.merchant}')",
    }

    if label:
        strong_str = " and ".join(dim_labels[d] for d in strong) if strong else "overall similarity"
        weak_str   = " and ".join(dim_labels[d] for d in weak)   if weak   else None

        msg = f"I classified this as '{label}' because the {strong_str} strongly match your previous '{label}' transactions"
        if weak_str:
            msg += f", even though the {weak_str} is weaker than usual"
        msg += "."
    else:
        parts_str = ", ".join(f"{d}={s:.2f}" for d, s in ranked)
        msg = (
            f"This transaction (merchant '{tx.merchant}', amount {tx.amount:,} {tx.currency}) "
            f"fits an unresolved cluster (purity={partition.purity:.0%}, support={partition.support}). "
            f"Dimension scores: {parts_str}. What category is this?"
        )

    return msg


# ---------------------------------------------------------------------------
# Public convenience: build everything fresh from SQLite
# ---------------------------------------------------------------------------

def run(
    classify_tx: dict[str, Any] | None = None,
) -> tuple[list[Partition], ClassificationResult | None]:
    """
    Build graph + partitions from current DB state.
    Optionally classify a single transaction dict (must include all fields).
    Returns (partitions, classification_result_or_None).
    """
    nodes = _load_nodes()
    G = build_graph(nodes)
    partitions = detect_partitions(G, nodes)

    result = None
    if classify_tx is not None:
        result = classify(classify_tx, nodes=nodes, G=G, partitions=partitions)

    return partitions, result
