"""Memory service — writes to and reads from Forma's brain.

Write path: extract_memories() runs a Haiku pass over a conversation exchange
or ride debrief, returning typed entities + edges (taxonomy prompt is a stable,
cacheable prefix). Dedup is by (type, normalized label) similarity — existing
entities are enriched, never duplicated.

Read paths:
- get_graph(): nodes + edges for the Brain page.
- get_context(): a compact text block for Forma's prompts (recent + connected
  + goal-adjacent), hidden facts included but flagged never-quote.

Extraction failures are logged loudly. Never swallowed silently — a memory
that silently fails to write is a relationship that silently decays.
"""

import json
import logging
from datetime import datetime
from difflib import SequenceMatcher

from app.core import forma_core
from sqlalchemy import func
from sqlalchemy.orm import Session

from app.core.memory_taxonomy import EDGE_TYPES, ENTITY_TYPES, LIFE_AREAS, extraction_prompt
from app.models.memory import MemoryEdge, MemoryEntity
from app.models.user import User
from app.core.llm_utils import response_text

logger = logging.getLogger(__name__)

DEDUP_THRESHOLD = 0.86


def _norm(s: str) -> str:
    return " ".join(s.lower().strip().split())


def _similar(a: str, b: str) -> float:
    return SequenceMatcher(None, _norm(a), _norm(b)).ratio()


def _find_existing(db: Session, user_id: str, etype: str, label: str) -> MemoryEntity | None:
    """Dedup: same type + near-identical label → same entity."""
    candidates = (
        db.query(MemoryEntity)
        .filter(MemoryEntity.user_id == user_id, MemoryEntity.type == etype)
        .all()
    )
    best, best_score = None, 0.0
    for c in candidates:
        s = _similar(c.label, label)
        if s > best_score:
            best, best_score = c, s
    return best if best_score >= DEDUP_THRESHOLD else None


def extract_memories(
    db: Session,
    user: User,
    text: str,
    source: str,
    source_ref: str | None = None,
    observed_at: datetime | None = None,
) -> dict:
    """Extract entities + edges from text and upsert into the graph.

    Returns {"created": int, "linked": int} for logging/telemetry.
    """
    if not text or len(text.strip()) < 40:
        return {"created": 0, "linked": 0}

    # Known entities give the model dedup + edge targets.
    known = (
        db.query(MemoryEntity)
        .filter(MemoryEntity.user_id == user.id)
        .order_by(MemoryEntity.updated_at.desc().nullslast(), MemoryEntity.created_at.desc())
        .limit(60)
        .all()
    )
    known_block = "\n".join(f"- [{e.type}] {e.label}" for e in known) or "(none yet)"

    try:
        resp = forma_core.call(
            user_id=user.id,
            task="memory_extraction",
            surface=source,
            system=[
                {
                    "type": "text",
                    "text": extraction_prompt(),
                    "cache_control": {"type": "ephemeral"},
                }
            ],
            messages=[
                {
                    "role": "user",
                    "content": (
                        f"KNOWN ENTITIES (dedup against these; you may create edges to them):\n"
                        f"{known_block}\n\n"
                        f"SOURCE: {source}\n\nTEXT:\n{text[:6000]}"
                    ),
                }
            ],
        )
        raw = response_text(resp).strip()
        if raw.startswith("```"):
            raw = raw.strip("`")
            raw = raw[raw.index("{"):]
        data = json.loads(raw[raw.index("{"): raw.rindex("}") + 1])
    except Exception:
        logger.exception("Memory extraction failed (user=%s source=%s)", user.id, source)
        return {"created": 0, "linked": 0}

    label_to_entity: dict[str, MemoryEntity] = {_norm(e.label): e for e in known}
    created = 0

    for ent in data.get("entities", []):
        etype = ent.get("type")
        label = (ent.get("label") or "").strip()[:255]
        if etype not in ENTITY_TYPES or not label:
            continue
        life_area = ent.get("life_area") if ent.get("life_area") in LIFE_AREAS else "training"
        kind = ent.get("kind")
        if kind and kind not in ENTITY_TYPES[etype]["kinds"]:
            kind = None

        existing = label_to_entity.get(_norm(label)) or _find_existing(db, user.id, etype, label)
        if existing:
            # Enrich, don't duplicate.
            if ent.get("summary") and not existing.summary:
                existing.summary = ent["summary"][:1000]
            existing.updated_at = datetime.utcnow()
            label_to_entity[_norm(label)] = existing
            continue

        node = MemoryEntity(
            user_id=user.id,
            type=etype,
            kind=kind,
            life_area=life_area,
            label=label,
            summary=(ent.get("summary") or "")[:1000] or None,
            status="noted" if etype == "insight" else None,
            visibility="private",
            source=source,
            source_ref=source_ref,
        )
        # Semantic fingerprint at write time — the RAG layer's index.
        try:
            from app.core import embeddings as emb

            vecs = emb.embed_texts([f"{label}. {node.summary or ''}".strip()])
            if vecs:
                node.embedding = vecs[0]
        except Exception:
            pass  # embeddings are an enhancement, never a blocker
        if observed_at is not None:
            # Backfill: date the memory when it actually happened, so the
            # Brain's growth replay tells the true story.
            node.observed_at = observed_at
        db.add(node)
        db.flush()
        label_to_entity[_norm(label)] = node
        created += 1

    linked = 0
    for edge in data.get("edges", []):
        et = edge.get("edge_type")
        if et not in EDGE_TYPES:
            continue
        a = label_to_entity.get(_norm(edge.get("from_label", "")))
        b = label_to_entity.get(_norm(edge.get("to_label", "")))
        if not a or not b or a.id == b.id:
            continue
        dup = (
            db.query(MemoryEdge)
            .filter(
                MemoryEdge.user_id == user.id,
                MemoryEdge.from_id == a.id,
                MemoryEdge.to_id == b.id,
                MemoryEdge.edge_type == et,
            )
            .first()
        )
        if dup:
            continue
        db.add(MemoryEdge(user_id=user.id, from_id=a.id, to_id=b.id, edge_type=et))
        linked += 1

    # Rider Dossier: typed personal facts extracted in the same pass.
    from app.services.dossier_service import upsert_entry

    dossier_added = 0
    for item in data.get("dossier", []):
        try:
            entry = upsert_entry(
                db,
                user.id,
                dimension=item.get("dimension", ""),
                content=item.get("content", ""),
                confidence=float(item.get("confidence", 0.6)),
                source=f"{source}:{source_ref}" if source_ref else source,
            )
            if entry is not None:
                dossier_added += 1
        except Exception:
            logger.exception("Dossier upsert failed (user=%s)", user.id)

    db.commit()
    if created or linked:
        logger.info(
            "Memory: +%d entities, +%d edges (user=%s source=%s)",
            created, linked, user.id, source,
        )
    return {"created": created, "linked": linked}


def get_graph(db: Session, user: User, include_hidden: bool = False) -> dict:
    """Nodes + edges for the Brain page."""
    q = db.query(MemoryEntity).filter(MemoryEntity.user_id == user.id)
    if not include_hidden:
        q = q.filter(MemoryEntity.hidden_at.is_(None))
    entities = q.order_by(MemoryEntity.created_at.asc()).all()
    ids = {e.id for e in entities}
    edges = (
        db.query(MemoryEdge)
        .filter(MemoryEdge.user_id == user.id)
        .all()
    )
    return {
        "entities": [
            {
                "id": e.id,
                "type": e.type,
                "kind": e.kind,
                "life_area": e.life_area,
                "label": e.label,
                "summary": e.summary,
                "status": e.status,
                "source": e.source,
                "source_ref": e.source_ref,
                "observed_at": e.observed_at.isoformat() if e.observed_at else None,
                "created_at": e.created_at.isoformat() if e.created_at else None,
            }
            for e in entities
        ],
        "edges": [
            {"id": g.id, "from_id": g.from_id, "to_id": g.to_id, "edge_type": g.edge_type}
            for g in edges
            if g.from_id in ids and g.to_id in ids
        ],
    }


def get_context(
    db: Session, user: User, limit: int = 24, query: str | None = None
) -> str:
    """Compact memory block for Forma's prompts.

    Without a query: recency + connectedness weighted (the stable digest).
    With a query: semantic retrieval — cosine similarity to the rider's
    message leads the score, so asking about saddle pain surfaces the
    saddle memories from months ago, not just whatever's recent. Falls
    back to the recency scorer when embeddings are unavailable.

    Hidden entities ARE included (Forma keeps coaching around a hidden
    injury) but flagged never-quote.
    """
    entities = (
        db.query(MemoryEntity)
        .filter(MemoryEntity.user_id == user.id)
        .all()
    )
    if not entities:
        return ""

    degree: dict[str, int] = {}
    for g in db.query(MemoryEdge).filter(MemoryEdge.user_id == user.id).all():
        degree[g.from_id] = degree.get(g.from_id, 0) + 1
        degree[g.to_id] = degree.get(g.to_id, 0) + 1

    now = datetime.utcnow()

    def base_score(e: MemoryEntity) -> float:
        age_days = max(1.0, (now - (e.updated_at or e.created_at)).total_seconds() / 86400)
        type_boost = {"goal": 3, "gap": 2.5, "procedural": 2.5, "value": 2, "habit": 1.5}.get(e.type, 1)
        return (degree.get(e.id, 0) + 1) * type_boost / (age_days ** 0.4)

    semantic: dict[str, float] = {}
    if query:
        from app.core import embeddings as emb

        qvec = emb.embed_query(query[:1000])
        if qvec is not None:
            sims = emb.cosine_scores(qvec, [e.embedding for e in entities])
            semantic = {e.id: s for e, s in zip(entities, sims)}

    if semantic:
        # Similarity leads; base score (normalised) keeps the load-bearing
        # memories (goals, rules) from vanishing on off-topic questions.
        max_base = max(base_score(e) for e in entities) or 1.0
        top = sorted(
            entities,
            key=lambda e: 0.7 * semantic.get(e.id, 0.0) + 0.3 * (base_score(e) / max_base),
            reverse=True,
        )[:limit]
        header = (
            "WHAT YOU REMEMBER, MOST RELEVANT TO THIS MESSAGE "
            "(long-term memory — weave in naturally):"
        )
    else:
        top = sorted(entities, key=base_score, reverse=True)[:limit]
        header = "WHAT YOU KNOW ABOUT THIS RIDER (long-term memory — weave in naturally):"

    lines = [header]
    for e in top:
        flag = " [HIDDEN — use for judgement, never mention]" if e.hidden_at else ""
        kind = f"/{e.kind}" if e.kind else ""
        lines.append(f"- ({e.type}{kind}) {e.label}{': ' + e.summary if e.summary else ''}{flag}")
    return "\n".join(lines)


def embed_missing_entities(db: Session, batch: int = 64) -> int:
    """Embed every memory entity that doesn't have a vector yet.

    Runs as a startup background task (also warms the model so the first
    chat message never pays the load cost). Returns how many were embedded.
    """
    from app.core import embeddings as emb

    if not emb.is_available():
        return 0
    done = 0
    while True:
        rows = (
            db.query(MemoryEntity)
            .filter(MemoryEntity.embedding.is_(None))
            .limit(batch)
            .all()
        )
        if not rows:
            break
        texts = [f"{e.label}. {e.summary or ''}".strip() for e in rows]
        vecs = emb.embed_texts(texts)
        if vecs is None:
            break
        for e, v in zip(rows, vecs):
            e.embedding = v
        db.commit()
        done += len(rows)
    if done:
        logger.info("Embedded %d memory entities", done)
    return done


def set_hidden(db: Session, user: User, entity_id: str, hidden: bool) -> MemoryEntity | None:
    e = (
        db.query(MemoryEntity)
        .filter(MemoryEntity.id == entity_id, MemoryEntity.user_id == user.id)
        .first()
    )
    if not e:
        return None
    e.hidden_at = datetime.utcnow() if hidden else None
    db.commit()
    return e
