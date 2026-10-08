"""The coach writing first.

Every hour this looks at each rider's activation stage and how long they have
been quiet at it. At one day, three days and seven days of silence it writes
them one email, in the coach's voice, built from their own goal and the single
next step, ending with a question. Each (stage, threshold) is sent once, so a
rider hears from the coach three times at most per step, and never twice for
the same reason.

Why it exists: on 2 Sep 2026 a rider had a good first conversation, the coach
said "next time we talk", and nothing in the product ever spoke first again.
"""

import asyncio
import json
import logging
from datetime import datetime

from sqlalchemy.orm import Session

from app.config import settings
from app.core import forma_core
from app.core.llm_utils import response_text
from app.models.outreach import OutreachLog
from app.models.user import User
from app.services import email_service
from app.services.activation_service import activation_state

logger = logging.getLogger(__name__)

THRESHOLDS = (1, 3, 7)  # quiet days at a stage before the coach writes
_task: asyncio.Task | None = None

BRIEF = """You are {coach}, a cycling coach writing a short email to one rider who has
gone quiet before finishing setting up. You are not marketing anything; you are
their coach noticing they stopped.

Write plain British English. Contractions are normal. No em dashes, no en
dashes (use full stops or colons, never a comma between two sentences). No exclamation marks. No metaphors
or flourish: say the literal thing. No "it's not X, it's Y" constructions. Never
open a sentence with Here's. Nothing that could sit in any fitness app's email.
Never invent a feature, a number or a fact that is not in the context. If it
comes up, say plainly that you are Forma's AI coach. Never claim to be a
person. You are the coach, writing in the first person; you are not Gareth,
the founder, and never sign as him. If you use a training term
(FTP, TSS, Z2 and the like), say what it means in plain words in the same
sentence. Make the decision for them: one step, not a menu of options.

Read `safety` in the context before anything else. If allowed_intensity is
anything other than "all", any riding you mention is easy riding by feel, with
no targets, no intervals and no tests. Treat any doctor_limits as hard limits.
Never play down anything in `safety`.

Shape, at most 170 words:
1. Open with something specific from their own words or goal, so it is
   obviously about them.
2. Say the one thing that is standing in the way, and exactly how to do it,
   using the navigation given in the context word for word.
3. Say plainly what happens once they do it.
4. End with one short question they can answer in a line, and ask them to
   answer it on the Coach page in Forma, where you will see it. (Replies to
   this email reach Gareth's inbox, not you.)
Sign off with the coach's name on its own line.

Return exactly this format:
Subject: <five to eight plain words, no punctuation at the end>

<body>"""


def _medical_restriction(db: Session, user_id: str) -> bool:
    """Whether a hold other than the easy start after a break, or a health
    answer still waiting for a doctor's clearance, limits this rider."""
    from app.services import safety_service
    from app.services.plan_review_service import live_holds

    if any(
        h.source != "layoff" and h.red_flag != "layoff" for h in live_holds(db, user_id)
    ):
        return True
    screening = safety_service.latest_screening(db, user_id)
    return (
        screening is not None
        and screening.tier != "none"
        and screening.clearance_confirmed_at is None
    )


def speak_first_block(db: Session, user: User | str, *, training: bool = True) -> str | None:
    """Why the coach must not raise anything with this rider unprompted right
    now, or None when it may. Outreach emails, initiative cards and pre-ride
    briefings all ask this one question.

    "minor": the account is held as possibly under 18. Nothing goes to it.
    "wellbeing": the rider wrote words of hopelessness or worse in the last
    14 days. Nothing presses them.
    "safety" (only when `training`, for anything that nudges towards riding):
    nothing at all may be prescribed today, or a hold or an uncleared health
    answer limits them. The layoff gate on its own does not block: easy riding
    is exactly what it prescribes, so the nudge goes out with the safety
    state in its brief.
    "unknown": the safety state could not be read. Fails closed."""
    from app.services import safety_screen, safety_service
    from app.services.plan_review_service import minor_hold_open

    uid = user if isinstance(user, str) else user.id
    try:
        if minor_hold_open(db, uid):
            return "minor"
        quiet = safety_screen.wellbeing_quiet_until(db, uid)
        if quiet is not None and quiet > datetime.utcnow():
            return "wellbeing"
        if not training:
            return None
        allowed = safety_service.allowed_intensity(db, user)
        if allowed == "all":
            return None
        if allowed == "none" or _medical_restriction(db, uid):
            return "safety"
        return None
    except Exception:
        logger.exception("Reading the safety state before writing first failed (user=%s)", uid)
        return "unknown"


def _threshold_due(quiet_days: int) -> int | None:
    """The highest threshold the rider has passed, or None."""
    passed = [t for t in THRESHOLDS if quiet_days >= t]
    return max(passed) if passed else None


def _already_sent(db: Session, user_id: str, stage: str, threshold: int) -> bool:
    return (
        db.query(OutreachLog.id)
        .filter(
            OutreachLog.user_id == user_id,
            OutreachLog.stage == stage,
            OutreachLog.threshold_days == threshold,
        )
        .first()
        is not None
    )


def due_riders(db: Session, now: datetime | None = None) -> list[tuple[User, dict, int]]:
    """Every active, verified rider who is stuck at a stage and has passed a
    threshold the coach has not yet written to them about."""
    now = now or datetime.utcnow()
    out = []
    riders = (
        db.query(User)
        .filter(
            User.is_active.is_(True),
            User.email_verified.is_(True),
            User.deleted_at.is_(None),
            User.coach_emails_off_at.is_(None),
        )
        .all()
    )
    from app.services.billing_service import has_access

    for user in riders:
        # The coach writes to members. A rider who hasn't joined, or whose
        # membership has ended, would be urged towards sessions they can't open.
        if not has_access(user):
            continue
        state = activation_state(db, user, now)
        if state["stage"] == "established" or not state["next_action"]:
            continue
        threshold = _threshold_due(state["quiet_days"])
        if threshold is None or _already_sent(db, user.id, state["stage"], threshold):
            continue
        # Every check-in pushes towards riding. Not to an account that may be
        # a child's, not to a rider in the quiet window after crisis words,
        # and not to a rider a hold or a health answer keeps off hard riding.
        block = speak_first_block(db, user, training=True)
        if block:
            logger.debug("Outreach held back for %s: %s", user.id, block)
            continue
        out.append((user, state, threshold))
    return out


def _rider_brief(db: Session, user: User, state: dict) -> dict:
    """What the coach may draw on. Their own words first."""
    from app.models.onboarding import GoalEvent, GoalStatus

    goal = (
        db.query(GoalEvent)
        .filter(GoalEvent.user_id == user.id, GoalEvent.status == GoalStatus.upcoming)
        .order_by(GoalEvent.event_date)
        .first()
    )
    brief = {
        "rider_first_name": (user.full_name or user.email.split("@")[0]).split()[0],
        "quiet_days": state["quiet_days"],
        "stage": state["stage"],
        "next_step_title": state["next_action"]["title"],
        "next_step_navigation": state["next_action"]["instruction"],
        "what_happens_after": {
            "goal": "Once the goal is set, every plan and briefing is built around it.",
            "data": "Once rides arrive, the coach can see how the rider actually rides and coach from real numbers instead of the setup answers.",
            "first_ride": "One ride is enough for the coach to start coaching from real numbers.",
            "plan": "Pressing Build my season writes the plan from today to the goal date, and it appears under Goal.",
            "first_week": "After the first week the coach adjusts the following week from what was actually ridden.",
        }.get(state["stage"], ""),
    }
    if goal is not None:
        brief["goal"] = {
            "event": goal.event_name,
            "date": goal.event_date.isoformat() if goal.event_date else None,
            "their_why": goal.why,
            "who_they_are_becoming": goal.becoming,
        }
    # What the rider may ride today, read like every other coach surface
    # reads it (SAFETY LAW rule 7). A failed read is said as unknown.
    try:
        from app.services.safety_screen import coach_safety_context

        safety = coach_safety_context(db, user)
        # The state, not the emergency numbers: a check-in email is no place
        # for them, and only riders with no medical restriction get one.
        for key in ("numbers", "triage_region", "country"):
            safety.pop(key, None)
        brief["safety"] = safety
    except Exception:
        logger.exception("Safety context for outreach failed (user=%s)", user.id)
        brief["safety"] = {
            "allowed_intensity": "unknown",
            "means": "The safety state could not be read. Suggest nothing above easy riding.",
        }
    return brief


def compose(db: Session, user: User, state: dict) -> tuple[str, str]:
    """Subject and body, written by the coach for this rider now."""
    coach = getattr(user, "coach_name", None) or "Forma"
    resp = forma_core.call(
        user_id=user.id,
        task="outreach_email",
        surface="outreach",
        system=BRIEF.format(coach=coach),
        messages=[{"role": "user", "content": json.dumps(_rider_brief(db, user, state), indent=2)}],
    )
    text = response_text(resp).strip()
    subject, body = _split(text, fallback_subject=state["next_action"]["title"])
    # Every check-in says it was written by AI and how to stop them; the
    # privacy policy promises the second, the EU AI Act the first.
    return subject, body.rstrip() + "\n\n" + email_footer(coach)


def email_footer(coach: str = "Forma") -> str:
    """The footer on every coach email (plan section D)."""
    who = "Forma, your AI coach" if coach == "Forma" else f"{coach}, your AI coach in Forma"
    return (
        f"Written by {who}. It can be wrong and isn't medical advice. "
        'Reply "stop" and the coach won\'t email you again.'
    )


def _split(text: str, fallback_subject: str) -> tuple[str, str]:
    lines = text.strip().splitlines()
    if lines and lines[0].lower().startswith("subject:"):
        subject = lines[0].split(":", 1)[1].strip().rstrip(".!") or fallback_subject
        body = "\n".join(lines[1:]).strip()
    else:
        subject, body = fallback_subject, text.strip()
    for dash in ("\u2014", "\u2013"):
        subject = subject.replace(dash, ",")
        body = body.replace(dash, ",")
    return subject[:200], body


async def send_due(db: Session, now: datetime | None = None, dry_run: bool = False) -> list[dict]:
    """Write to everyone who is due. Returns what was (or would be) sent."""
    report = []
    for user, state, threshold in due_riders(db, now):
        try:
            subject, body = compose(db, user, state)
        except forma_core.BudgetExceededError:
            logger.info("Outreach skipped for %s: over monthly Forma budget", user.id)
            continue
        except Exception:
            logger.exception("Outreach compose failed for %s", user.id)
            continue
        entry = {
            "user_id": user.id, "email": user.email, "stage": state["stage"],
            "threshold_days": threshold, "subject": subject, "body": body,
        }
        if not dry_run:
            ok = await email_service.send(user.email, subject, body)
            if ok:
                db.add(OutreachLog(
                    user_id=user.id, stage=state["stage"], threshold_days=threshold,
                    subject=subject, body=body,
                ))
                db.commit()
                logger.info(
                    "Outreach sent to %s (stage=%s, %sd quiet)", user.id, state["stage"], threshold
                )
            entry["sent"] = ok
        report.append(entry)
    return report


async def _loop(interval: int) -> None:
    from app.database import SessionLocal

    logger.info("Outreach engine started (every %ds)", interval)
    await asyncio.sleep(60)
    while True:
        db = SessionLocal()
        try:
            sent = await send_due(db)
            if sent:
                logger.info("Outreach: %d email(s) sent", len(sent))
        except asyncio.CancelledError:
            db.close()
            return
        except Exception:
            logger.exception("Outreach loop error")
        finally:
            db.close()
        try:
            await asyncio.sleep(interval)
        except asyncio.CancelledError:
            return


def start_outreach(interval: int = 3600) -> None:
    global _task
    if _task is not None and not _task.done():
        return
    _task = asyncio.create_task(_loop(interval))
