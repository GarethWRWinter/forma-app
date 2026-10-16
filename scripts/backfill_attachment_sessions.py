"""Give files shared before 16 Oct 2026 back to the chat they were sent in.

Until then nothing set chat_attachments.session_id, so a file reached the
coach on the turn it was attached and never again. The record of where each
one went survives in chat_messages.context_snapshot: the coach's reply on the
turn a file arrived stored its whole context, files included, under
"attachments", and from this release a rider message stores the ids it
carried under "attachment_ids". This reads both and ties each untied file to
the earliest chat that carried it, the same rule the live send follows.

It never ties a file to a chat that belongs to someone other than the file's
own rider; those claims are counted and refused. Dry run by default: it
prints counts and writes nothing. --apply writes. Running it twice changes
nothing the second time, because a file that already has a chat is never
moved.

Usage:
    python scripts/backfill_attachment_sessions.py
    python scripts/backfill_attachment_sessions.py --apply
"""

import argparse
import sys
from dataclasses import dataclass, field
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.models.chat import ChatMessage, ChatSession  # noqa: E402
from app.models.chat_attachment import ChatAttachment  # noqa: E402


@dataclass
class Plan:
    # attachment id -> the chat it will be tied to
    bindings: dict[str, str] = field(default_factory=dict)
    # attachment id -> its rider, for every file still without a chat
    unbound: dict[str, str] = field(default_factory=dict)
    # files a message in someone else's chat claimed; never tied there
    refused: set[str] = field(default_factory=set)


def _carried_ids(snapshot) -> list[str]:
    """The file ids one message's snapshot says it carried."""
    if not isinstance(snapshot, dict):
        return []
    ids = snapshot.get("attachment_ids")
    found = [i for i in ids if isinstance(i, str)] if isinstance(ids, list) else []
    files = snapshot.get("attachments")
    if isinstance(files, list):
        found += [
            f["attachment_id"]
            for f in files
            if isinstance(f, dict) and isinstance(f.get("attachment_id"), str)
        ]
    return found


def plan(db) -> Plan:
    """Where each untied file goes: the earliest of its own rider's chats
    whose messages carried it."""
    result = Plan(
        unbound=dict(
            db.query(ChatAttachment.id, ChatAttachment.user_id)
            .filter(ChatAttachment.session_id.is_(None))
            .all()
        )
    )
    if not result.unbound:
        return result

    messages = (
        db.query(ChatMessage.session_id, ChatSession.user_id, ChatMessage.context_snapshot)
        .join(ChatSession, ChatSession.id == ChatMessage.session_id)
        .filter(ChatMessage.context_snapshot.isnot(None))
        .order_by(ChatMessage.created_at, ChatMessage.id)
        .yield_per(200)
    )
    for session_id, chat_owner, snapshot in messages:
        for aid in _carried_ids(snapshot):
            owner = result.unbound.get(aid)
            if owner is None or aid in result.bindings:
                continue
            if owner == chat_owner:
                result.bindings[aid] = session_id
            else:
                result.refused.add(aid)
    return result


def apply(db, bindings: dict[str, str], unbound: dict[str, str]) -> int:
    """Write the bindings. Each one re-checks that the file is still untied
    and that the chat is its own rider's."""
    owners = dict(
        db.query(ChatSession.id, ChatSession.user_id)
        .filter(ChatSession.id.in_(sorted(set(bindings.values()))))
        .all()
    ) if bindings else {}
    tied = 0
    for aid, session_id in bindings.items():
        owner = unbound.get(aid)
        if owner is None or owners.get(session_id) != owner:
            continue
        tied += (
            db.query(ChatAttachment)
            .filter(
                ChatAttachment.id == aid,
                ChatAttachment.user_id == owner,
                ChatAttachment.session_id.is_(None),
            )
            .update({ChatAttachment.session_id: session_id}, synchronize_session=False)
        )
    db.commit()
    return tied


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--apply", action="store_true", help="write the bindings")
    args = parser.parse_args()

    from app.database import SessionLocal

    db = SessionLocal()
    try:
        found = plan(db)
        print(f"{len(found.unbound)} files without a chat")
        print(f"{len(found.bindings)} traced to their own rider's chat")
        refused = len(found.refused - set(found.bindings))
        print(f"{refused} claimed only by another rider's chat, refused")
        if args.apply:
            print(f"{apply(db, found.bindings, found.unbound)} tied")
        else:
            print("Dry run: nothing written. Pass --apply to tie them.")
    finally:
        db.close()


if __name__ == "__main__":
    main()
