"""Memory API, the Brain.

GET  /memory/graph       → nodes + edges for the Brain page
GET  /memory/reading     → Forma's narrated reading of the graph
PATCH /memory/{id}/hide  → hide-not-delete
PATCH /memory/{id}/unhide
"""

import logging

from app.core import forma_core
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app.api.v1.deps import get_current_user, get_db
from app.models.user import User
from app.services import memory_service
from app.core.llm_utils import response_text
from app.core.coach_skills import distilled_persona

router = APIRouter(prefix="/memory", tags=["memory"])
logger = logging.getLogger(__name__)

@router.get("/graph")
def get_memory_graph(
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    return memory_service.get_graph(db, current_user)


@router.get("/reading")
def get_memory_reading(
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Forma's short narrated reading of the rider's brain."""
    context = memory_service.get_context(db, current_user, limit=40)
    if not context:
        return {"reading": None}
    try:
        resp = forma_core.call(
            user_id=current_user.id,
            task="memory_reading",
            surface="brain",
            system=(
                distilled_persona(current_user.coach_name, current_user.coach_tone)
                + "\n\n## This surface: Forma's reading of the rider's brain\n"
                "You are looking at their memory graph, everything you know about them. "
                "Write a 2-3 sentence reading: name the strongest cluster or thread, "
                "point out one meaningful cross-life connection or one quiet corner "
                "worth attention. Speak to the rider as 'you'. No preamble, no sign-off."
            ),
            messages=[{"role": "user", "content": context}],
        )
        return {"reading": response_text(resp).strip()}
    except Exception:
        logger.exception("Memory reading failed (user=%s)", current_user.id)
        return {"reading": None}


@router.patch("/{entity_id}/hide")
def hide_entity(
    entity_id: str,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    e = memory_service.set_hidden(db, current_user, entity_id, True)
    if not e:
        raise HTTPException(status_code=404, detail="Memory not found")
    return {"id": e.id, "hidden": True}


@router.patch("/{entity_id}/unhide")
def unhide_entity(
    entity_id: str,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    e = memory_service.set_hidden(db, current_user, entity_id, False)
    if not e:
        raise HTTPException(status_code=404, detail="Memory not found")
    return {"id": e.id, "hidden": False}
