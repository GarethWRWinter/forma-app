"""Rider Dossier service: what Forma knows, and what it's still curious about.

The dossier powers hyper-personalization two ways:
1. `dossier_context()` puts what the coach KNOWS into every prompt.
2. `gaps()` tells the coach what it DOESN'T know yet, feeding the
   one-question-at-a-time drip, the coach earns the dossier through
   natural curiosity, never a questionnaire.
"""

import logging
from difflib import SequenceMatcher

from sqlalchemy.orm import Session

from app.models.dossier import DOSSIER_DIMENSIONS, DossierEntry

logger = logging.getLogger(__name__)

# An entry this similar to an existing one on the same dimension is a
# restatement, not new knowledge, refresh instead of duplicating.
_DUP_THRESHOLD = 0.82

# Cap per dimension so the prompt block stays lean; oldest inactive first.
_MAX_ACTIVE_PER_DIMENSION = 3


def get_active_entries(db: Session, user_id: str) -> list[DossierEntry]:
    return (
        db.query(DossierEntry)
        .filter(DossierEntry.user_id == user_id, DossierEntry.active.is_(True))
        .order_by(DossierEntry.dimension, DossierEntry.created_at.desc())
        .all()
    )


def upsert_entry(
    db: Session,
    user_id: str,
    dimension: str,
    content: str,
    confidence: float = 0.6,
    source: str | None = None,
) -> DossierEntry | None:
    """Add one learned fact. Near-duplicates refresh the existing entry."""
    if dimension not in DOSSIER_DIMENSIONS:
        return None
    content = (content or "").strip()
    if len(content) < 8:
        return None
    confidence = max(0.1, min(1.0, confidence))

    existing = (
        db.query(DossierEntry)
        .filter(
            DossierEntry.user_id == user_id,
            DossierEntry.dimension == dimension,
            DossierEntry.active.is_(True),
        )
        .all()
    )
    for e in existing:
        ratio = SequenceMatcher(None, e.content.lower(), content.lower()).ratio()
        if ratio >= _DUP_THRESHOLD:
            # Restatement: keep the richer wording, lift confidence.
            if len(content) > len(e.content):
                e.content = content[:2000]
            e.confidence = max(e.confidence, confidence)
            if source:
                e.source = source
            db.flush()
            return e

    entry = DossierEntry(
        user_id=user_id,
        dimension=dimension,
        content=content[:2000],
        confidence=confidence,
        source=source,
    )
    db.add(entry)
    db.flush()

    # Keep each dimension lean: retire the oldest beyond the cap.
    if len(existing) + 1 > _MAX_ACTIVE_PER_DIMENSION:
        overflow = sorted(existing, key=lambda e: e.created_at or "")[
            : len(existing) + 1 - _MAX_ACTIVE_PER_DIMENSION
        ]
        for e in overflow:
            e.active = False
    return entry


def gaps(db: Session, user_id: str, limit: int = 4) -> list[tuple[str, str]]:
    """Dimensions the coach knows nothing about yet, in priority order."""
    covered = {
        d for (d,) in (
            db.query(DossierEntry.dimension)
            .filter(DossierEntry.user_id == user_id, DossierEntry.active.is_(True))
            .distinct()
            .all()
        )
    }
    out = [
        (dim, desc)
        for dim, desc in DOSSIER_DIMENSIONS.items()
        if dim not in covered
    ]
    return out[:limit]


def dossier_context(db: Session, user_id: str) -> str:
    """Prompt-ready block: what the coach knows + what it's curious about."""
    entries = get_active_entries(db, user_id)
    lines: list[str] = []
    if entries:
        lines.append("## The Rider's Dossier (learned over time, use it, naturally)")
        current = None
        for e in entries:
            if e.dimension != current:
                current = e.dimension
                lines.append(f"### {current}")
            lines.append(f"- {e.content}")
    open_gaps = gaps(db, user_id)
    if open_gaps:
        lines.append("\n## Dossier gaps (things you're quietly curious about)")
        for dim, desc in open_gaps:
            lines.append(f"- {dim}: {desc}")
        lines.append(
            "\nCuriosity rule: if, and only if, the conversation makes it natural, "
            "end your reply with ONE short question drawn from these gaps. Never more "
            "than one. Never interrogate. If the moment doesn't invite it, ask nothing."
        )
    return "\n".join(lines)
