"""
Yaazhi V4 — Neo4j Knowledge Mesh (Layer 3, 4, 5, 8).

Implements the Knowledge Mesh Schema described in the V4 framework:

  Node taxonomy: (:Identity), (:Goal), (:Fact), (:Decision), (:Task)

  Relationships:
    (:Identity)-[:ALIGNS_WITH]->(:Goal)
    (:Goal)-[:HAS_SUBGOAL]->(:Goal)
    (:Goal)-[:RESOLVED_BY]->(:Decision)
    (:Decision)-[:DERIVED_FROM_FACT]->(:Fact)
    (:Fact)-[:BELONGS_TO_TINAI {name: <tinai>}]->(:Context)

Provides:
  - GraphStore.add_fact()          — Layer 3 Semantic Fact insertion.
  - GraphStore.add_goal()          — Layer 5 Active Goal insertion.
  - GraphStore.add_decision()      — Layer 6 Decision Ledger mirroring.
  - GraphStore.traverse()          — 2-hop graph traversal (context assembly).
  - GraphStore.supersede_node()    — Mark a node SUPERSEDED (reflection pipeline).
  - GraphStore.update_edge_weight()— Reinforce / weaken relationship confidence.
"""

from __future__ import annotations

import os
import uuid
from datetime import datetime, timezone
from typing import Any, Optional

import logfire

try:
    from neo4j import AsyncGraphDatabase, AsyncDriver  # type: ignore
    _NEO4J_AVAILABLE = True
except ImportError:
    _NEO4J_AVAILABLE = False


# ─── Node / Relationship Constants ───────────────────────────────────────────

class NodeLabel:
    IDENTITY = "Identity"
    GOAL     = "Goal"
    FACT     = "Fact"
    DECISION = "Decision"
    TASK     = "Task"
    CONTEXT  = "Context"


class RelType:
    ALIGNS_WITH        = "ALIGNS_WITH"
    HAS_SUBGOAL        = "HAS_SUBGOAL"
    RESOLVED_BY        = "RESOLVED_BY"
    DERIVED_FROM_FACT  = "DERIVED_FROM_FACT"
    BELONGS_TO_TINAI   = "BELONGS_TO_TINAI"
    SUPERSEDED_BY      = "SUPERSEDED_BY"


# ─── GraphStore ───────────────────────────────────────────────────────────────

class GraphStore:
    """
    Async Neo4j graph interface for the Yaazhi Knowledge Mesh.

    All write methods are idempotent (MERGE on node_id).
    Falls back gracefully if Neo4j is unavailable — logs a warning and
    returns empty / None so the rest of the pipeline is not blocked.

    Configuration via environment variables:
        NEO4J_URI      — bolt://host:7687
        NEO4J_USER     — default: neo4j
        NEO4J_PASSWORD — required
    """

    def __init__(self) -> None:
        self._driver: Optional[Any] = None
        self._uri      = os.getenv("NEO4J_URI",      "bolt://localhost:7687")
        self._user     = os.getenv("NEO4J_USER",     "neo4j")
        self._password = os.getenv("NEO4J_PASSWORD",  "")

    async def _ensure_driver(self) -> Optional[Any]:
        """Lazily initialise the Neo4j async driver."""
        if self._driver is not None:
            return self._driver
        if not _NEO4J_AVAILABLE:
            logfire.warning("GraphStore: neo4j driver not installed — skipping graph ops")
            return None
        if not self._password:
            logfire.warning("GraphStore: NEO4J_PASSWORD not set — graph ops disabled")
            return None
        try:
            self._driver = AsyncGraphDatabase.driver(
                self._uri, auth=(self._user, self._password)
            )
            logfire.info("GraphStore: Neo4j driver initialised", uri=self._uri)
        except Exception as exc:
            logfire.error("GraphStore: failed to connect to Neo4j", error=str(exc))
            self._driver = None
        return self._driver

    async def ping(self) -> bool:
        """Return True if Neo4j is reachable."""
        driver = await self._ensure_driver()
        if driver is None:
            return False
        try:
            await driver.verify_connectivity()
            return True
        except Exception:
            return False

    # ------------------------------------------------------------------
    # Write — Layer 3: Semantic Facts
    # ------------------------------------------------------------------

    async def add_fact(
        self,
        text: str,
        tinai: str = "general_technical",
        confidence: float = 0.9,
        source: str = "episodic_abstraction",
        node_id: Optional[str] = None,
    ) -> Optional[str]:
        """
        MERGE a :Fact node and attach it to its :Context (Tiṇai).

        Args:
            text:       The factual statement.
            tinai:      Tiṇai category string (e.g. 'arch_linux_dev').
            confidence: Abstraction confidence (must be >= 0.85 per V4 spec).
            source:     Origin of the fact.
            node_id:    Optional stable ID; generated if absent.

        Returns:
            The node_id of the created/merged Fact node, or None on failure.
        """
        driver = await self._ensure_driver()
        if driver is None:
            return None

        fid = node_id or str(uuid.uuid4())
        now = datetime.now(timezone.utc).isoformat()

        query = f"""
        MERGE (f:{NodeLabel.FACT} {{node_id: $node_id}})
        ON CREATE SET f.text = $text,
                      f.confidence = $confidence,
                      f.source = $source,
                      f.created_at = $now,
                      f.superseded = false
        ON MATCH SET  f.confidence = $confidence,
                      f.updated_at = $now
        WITH f
        MERGE (ctx:{NodeLabel.CONTEXT} {{name: $tinai}})
        MERGE (f)-[r:{RelType.BELONGS_TO_TINAI}]->(ctx)
        ON CREATE SET r.weight = 1.0
        RETURN f.node_id AS node_id
        """
        try:
            async with driver.session() as session:
                result = await session.run(
                    query,
                    node_id=fid, text=text, confidence=confidence,
                    source=source, now=now, tinai=tinai,
                )
                record = await result.single()
                logfire.debug("GraphStore.add_fact", node_id=fid, tinai=tinai)
                return record["node_id"] if record else fid
        except Exception as exc:
            logfire.error("GraphStore.add_fact failed", error=str(exc))
            return None

    # ------------------------------------------------------------------
    # Write — Layer 5: Active Goals
    # ------------------------------------------------------------------

    async def add_goal(
        self,
        title: str,
        description: str = "",
        parent_goal_id: Optional[str] = None,
        identity_id: Optional[str] = None,
        node_id: Optional[str] = None,
    ) -> Optional[str]:
        """
        MERGE a :Goal node, optionally linking to a parent :Goal or :Identity.

        Returns:
            node_id of the Goal node.
        """
        driver = await self._ensure_driver()
        if driver is None:
            return None

        gid = node_id or str(uuid.uuid4())
        now = datetime.now(timezone.utc).isoformat()

        base_query = f"""
        MERGE (g:{NodeLabel.GOAL} {{node_id: $node_id}})
        ON CREATE SET g.title = $title,
                      g.description = $description,
                      g.created_at = $now,
                      g.resolved = false
        ON MATCH SET  g.title = $title, g.updated_at = $now
        RETURN g.node_id AS node_id
        """
        try:
            async with driver.session() as session:
                await session.run(
                    base_query,
                    node_id=gid, title=title, description=description, now=now,
                )
                if parent_goal_id:
                    await session.run(
                        f"""
                        MATCH (parent:{NodeLabel.GOAL} {{node_id: $pid}})
                        MATCH (child:{NodeLabel.GOAL}  {{node_id: $cid}})
                        MERGE (parent)-[:{RelType.HAS_SUBGOAL}]->(child)
                        """,
                        pid=parent_goal_id, cid=gid,
                    )
                if identity_id:
                    await session.run(
                        f"""
                        MATCH (id:{NodeLabel.IDENTITY} {{node_id: $iid}})
                        MATCH (g:{NodeLabel.GOAL}      {{node_id: $gid}})
                        MERGE (id)-[:{RelType.ALIGNS_WITH}]->(g)
                        """,
                        iid=identity_id, gid=gid,
                    )
            logfire.debug("GraphStore.add_goal", node_id=gid)
            return gid
        except Exception as exc:
            logfire.error("GraphStore.add_goal failed", error=str(exc))
            return None

    # ------------------------------------------------------------------
    # Write — Layer 6: Decisions Ledger (graph mirror)
    # ------------------------------------------------------------------

    async def add_decision(
        self,
        topic: str,
        chosen_path: str,
        reasoning: str = "",
        goal_id: Optional[str] = None,
        fact_id: Optional[str] = None,
        node_id: Optional[str] = None,
    ) -> Optional[str]:
        """
        MERGE a :Decision node with optional links to :Goal and :Fact.

        Returns:
            node_id of the Decision node.
        """
        driver = await self._ensure_driver()
        if driver is None:
            return None

        did = node_id or str(uuid.uuid4())
        now = datetime.now(timezone.utc).isoformat()

        try:
            async with driver.session() as session:
                await session.run(
                    f"""
                    MERGE (d:{NodeLabel.DECISION} {{node_id: $node_id}})
                    ON CREATE SET d.topic = $topic,
                                  d.chosen_path = $chosen_path,
                                  d.reasoning = $reasoning,
                                  d.created_at = $now
                    ON MATCH SET  d.updated_at = $now
                    """,
                    node_id=did, topic=topic, chosen_path=chosen_path,
                    reasoning=reasoning, now=now,
                )
                if goal_id:
                    await session.run(
                        f"""
                        MATCH (g:{NodeLabel.GOAL}     {{node_id: $gid}})
                        MATCH (d:{NodeLabel.DECISION} {{node_id: $did}})
                        MERGE (g)-[:{RelType.RESOLVED_BY}]->(d)
                        """,
                        gid=goal_id, did=did,
                    )
                if fact_id:
                    await session.run(
                        f"""
                        MATCH (d:{NodeLabel.DECISION} {{node_id: $did}})
                        MATCH (f:{NodeLabel.FACT}     {{node_id: $fid}})
                        MERGE (d)-[:{RelType.DERIVED_FROM_FACT}]->(f)
                        """,
                        did=did, fid=fact_id,
                    )
            logfire.debug("GraphStore.add_decision", node_id=did)
            return did
        except Exception as exc:
            logfire.error("GraphStore.add_decision failed", error=str(exc))
            return None

    # ------------------------------------------------------------------
    # Read — 2-Hop Traversal (Context Assembly Step 3)
    # ------------------------------------------------------------------

    async def traverse(
        self,
        concept: str,
        tinai: Optional[str] = None,
        hops: int = 2,
        limit: int = 20,
    ) -> list[dict[str, Any]]:
        """
        2-hop graph traversal around a seed concept node.

        Used during context assembly (Step 3) to surface related facts,
        goals, and decisions within the 2,048-token budget.

        Args:
            concept: Text fragment to fuzzy-match against node labels/text.
            tinai:   Optional Tiṇai filter applied at the seed node level.
            hops:    Traversal depth (default 2 as per V4 spec).
            limit:   Max nodes returned.

        Returns:
            List of node property dicts for injection into the context block.
        """
        driver = await self._ensure_driver()
        if driver is None:
            return []

        tinai_filter = "AND (seed.tinai = $tinai OR NOT EXISTS(seed.tinai))" if tinai else ""

        query = f"""
        MATCH (seed)
        WHERE (seed.text CONTAINS $concept OR seed.title CONTAINS $concept)
        {tinai_filter}
        CALL apoc.path.subgraphNodes(seed, {{maxLevel: $hops, limit: $limit}})
        YIELD node
        RETURN labels(node)[0] AS label,
               node.node_id    AS node_id,
               COALESCE(node.text, node.title, node.topic, '') AS content,
               node.confidence AS confidence,
               node.created_at AS created_at
        LIMIT $limit
        """
        try:
            async with driver.session() as session:
                result = await session.run(
                    query,
                    concept=concept, tinai=tinai, hops=hops, limit=limit,
                )
                records = [dict(r) async for r in result]
                logfire.debug(
                    "GraphStore.traverse",
                    concept=concept[:40],
                    nodes_found=len(records),
                )
                return records
        except Exception as exc:
            # APOC may not be installed — fall back to simple 1-hop match
            logfire.warning("GraphStore.traverse (APOC fallback)", error=str(exc))
            return await self._simple_traverse(concept, tinai, limit)

    async def _simple_traverse(
        self, concept: str, tinai: Optional[str], limit: int
    ) -> list[dict[str, Any]]:
        """1-hop fallback when APOC is unavailable."""
        driver = await self._ensure_driver()
        if driver is None:
            return []
        query = """
        MATCH (seed)-[*1..2]-(neighbor)
        WHERE seed.text CONTAINS $concept OR seed.title CONTAINS $concept
        RETURN labels(neighbor)[0]                                        AS label,
               neighbor.node_id                                           AS node_id,
               COALESCE(neighbor.text, neighbor.title, neighbor.topic, '') AS content,
               neighbor.confidence                                         AS confidence
        LIMIT $limit
        """
        try:
            async with driver.session() as session:
                result = await session.run(query, concept=concept, limit=limit)
                return [dict(r) async for r in result]
        except Exception as exc2:
            logfire.error("GraphStore._simple_traverse failed", error=str(exc2))
            return []

    # ------------------------------------------------------------------
    # Reflection — mark node superseded, update edge weights
    # ------------------------------------------------------------------

    async def supersede_node(self, node_id: str, superseded_by_id: str) -> None:
        """
        Mark a node as SUPERSEDED and create a :SUPERSEDED_BY relationship.

        Called by the asynchronous reflection pipeline (Step 5) when a
        newer fact contradicts or replaces an older one.
        """
        driver = await self._ensure_driver()
        if driver is None:
            return
        try:
            async with driver.session() as session:
                await session.run(
                    f"""
                    MATCH (old  {{node_id: $old_id}})
                    MATCH (new  {{node_id: $new_id}})
                    SET   old.superseded = true
                    MERGE (old)-[:{RelType.SUPERSEDED_BY}]->(new)
                    """,
                    old_id=node_id, new_id=superseded_by_id,
                )
            logfire.info("GraphStore.supersede_node", old=node_id[:8], new=superseded_by_id[:8])
        except Exception as exc:
            logfire.error("GraphStore.supersede_node failed", error=str(exc))

    async def update_edge_weight(
        self, from_id: str, to_id: str, rel_type: str, delta: float = 0.1
    ) -> None:
        """
        Increment or decrement an edge's `weight` property.

        Used by the reflection pipeline to reinforce or weaken graph
        relationships based on observed co-occurrence in episodic events.

        Args:
            from_id:  Source node_id.
            to_id:    Target node_id.
            rel_type: Relationship type string (e.g. 'DERIVED_FROM_FACT').
            delta:    Signed weight delta (positive = reinforce, negative = weaken).
        """
        driver = await self._ensure_driver()
        if driver is None:
            return
        query = f"""
        MATCH (a {{node_id: $from_id}})-[r:{rel_type}]->(b {{node_id: $to_id}})
        SET r.weight = COALESCE(r.weight, 1.0) + $delta
        """
        try:
            async with driver.session() as session:
                await session.run(query, from_id=from_id, to_id=to_id, delta=delta)
            logfire.debug("GraphStore.update_edge_weight", rel=rel_type, delta=delta)
        except Exception as exc:
            logfire.error("GraphStore.update_edge_weight failed", error=str(exc))

    async def close(self) -> None:
        """Close the Neo4j driver connection pool."""
        if self._driver:
            await self._driver.close()
            self._driver = None


# ─── Module-level singleton ───────────────────────────────────────────────────
graph_store = GraphStore()
