"""
AI Coach service powered by Claude.

Assembles rider context, manages chat sessions, and streams
responses via Claude API with SSE.
"""

import base64
import json
import logging
import re
from datetime import date, datetime, timedelta, timezone

logger = logging.getLogger(__name__)

import anthropic  # kept for anthropic.APIError handling; calls go via forma_core
from sqlalchemy.orm import Session

from collections import Counter

from app.core import forma_core
from app.core.formulas import rider_profile_scores, rider_type_profile, w_per_kg as calc_w_per_kg
from app.models.chat import ChatMessage, ChatRole, ChatSession
from app.models.training import Workout, WorkoutStatus
from app.models.user import User
from app.services.metrics_service import (
    get_all_time_power_profile,
    get_current_fitness,
    get_ftp_history,
    get_pmc_data,
    get_weekly_training_load,
)
from app.services.onboarding_service import get_goals, get_onboarding_response
from app.services.plan_service import get_plans, get_workouts_by_date
from app.services.ride_service import get_rides
from app.services.zone_service import get_zones
from app.core.llm_utils import StreamHumanizer, humanize, response_text

# === System Prompt ===

from app.core.coach_skills import compose_education

# App-specific playbook: data triggers, plan tools, debrief protocol, format.
COACH_APP_PLAYBOOK = """## Proactive Coaching Triggers

When you see concerning patterns in the rider's data or conversation, proactively address them:

- **Overtraining risk**: TSB below -25 → suggest recovery, probe for symptoms (fatigue, irritability, poor sleep, elevated resting HR)
- **High ramp rate**: CTL increasing >7 TSS/week → warn about injury and illness risk, suggest a recovery week
- **Low compliance**: <70% of planned workouts completed → explore barriers with curiosity, not judgment. Are the workouts too hard? Too long? Is life getting in the way?
- **FTP plateau**: No improvement in 8+ weeks → suggest an FTP test, a training approach change, or explore whether recovery/nutrition/sleep is the limiter
- **Excessive intensity**: Too many Zone 4-5 days without Zone 1-2 recovery → recommend easy days and explain why
- **Race approaching**: Event within 14 days → shift to taper advice, race-day planning, mental preparation, and pacing strategy
- **Life stress signals**: Rider mentions work pressure, relationship issues, poor sleep, or general fatigue → acknowledge impact and adjust training expectations
- **Motivation decline**: Shorter messages, less enthusiasm, avoiding training discussion → gently check in on how they're feeling about cycling and life in general
- **Phase transitions**: Moving between training phases → guide the rider through the psychological shift (e.g., base phase feels boring but it's building the engine)

## Modifying the Training Plan

You have tools to modify the rider's training plan directly. Use them when the conversation leads to agreed changes:

- **update_workout**: Change a workout's title, description, type, date, duration, or TSS. Use the workout IDs from the `this_week` context.
- **swap_workout_date**: Swap the dates of two workouts to rearrange the week.
- **add_workout**: Add a new session to the plan.
- **skip_workout**: Mark a workout as skipped.

**When to use tools:**
- The rider asks to change their plan ("Can we swap Tuesday and Thursday?", "I want to skip tomorrow's session", "Add a recovery ride on Friday")
- You recommend a change and the rider agrees ("Let's do that", "Sounds good, make the change")
- Always confirm with the rider before making changes — describe what you'll do, then act

**When NOT to use tools:**
- General discussion about training philosophy or future plans
- The rider is just asking questions, not requesting changes
- Changes that affect weeks beyond the current week (explain you can only modify this week's plan)

**After using a tool**, briefly confirm what was changed and explain how it fits the overall training plan.

## Post-Event Debrief

When a rider has recently completed a goal event, proactively offer to debrief:
- Acknowledge the achievement — completing an event matters regardless of result
- Analyse their self-assessment alongside the actual ride data
- Compare planned vs actual: pacing, power fade, nutrition
- Connect the result to the training block — what worked in preparation?
- Identify 2-3 actionable takeaways for next time
- Discuss recovery plan and what's next
- Process disappointment constructively — it's data, not failure

## Response Format

- Keep responses concise and actionable unless the rider asks for a deep dive
- Use the rider's actual numbers from context — never speak in vague generalities
- When prescribing workouts, describe them clearly with power targets as % of FTP, duration, recovery intervals, and the purpose of the session
- Ask clarifying questions before prescribing when the situation is ambiguous
- When a rider is struggling, lead with empathy before solutions
- Use analogies and stories to make training concepts tangible
"""

# Forma's full education (app/core/coach_skills.py) + the app playbook.
COACH_SYSTEM_PROMPT = compose_education() + "\n\n" + COACH_APP_PLAYBOOK


def _system_blocks(user: User, dynamic: str, volatile: str | None = None) -> list:
    """System as [cached personalised education] + [per-turn dynamic context].

    The education is personalised (coach name + tone) but stable per user, so
    cache_control still hits on every follow-up turn (~90% cheaper, faster
    time-to-first-token).
    """
    education = compose_education(
        getattr(user, "coach_name", None) or "Forma",
        getattr(user, "coach_tone", None),
    )
    # Pin the rider's identity hard: models invent plausible names when a
    # name feels unusual. This is non-negotiable, so it lives in the stable
    # (cached) block, not the per-turn context.
    rider_name = ((user.full_name or user.email.split("@")[0]).split() or ["Rider"])[0]
    identity = (
        f"THE RIDER'S NAME IS {rider_name}. Address them as {rider_name} and "
        f"nothing else — never invent, substitute or vary their name."
    )
    blocks = [
        {
            "type": "text",
            "text": identity + "\n\n" + education + "\n\n" + COACH_APP_PLAYBOOK,
            "cache_control": {"type": "ephemeral"},
        },
        # The rider context is stable WITHIN a conversation (fitness, plan,
        # dossier only move when data moves), so cache this block too:
        # turn 2+ reads the whole system from cache — faster first token,
        # ~90% cheaper. A mid-conversation data change just re-caches once.
        {
            "type": "text",
            "text": dynamic,
            "cache_control": {"type": "ephemeral"},
        },
    ]
    # Per-turn block (RAG): small, changes every message, deliberately
    # UNCACHED so it never invalidates the big cached blocks above.
    if volatile:
        blocks.append({"type": "text", "text": volatile})
    return blocks


def _relevant_memories(db: Session, user: User, message: str) -> str | None:
    """Semantic recall for THIS message (the RAG layer's read path).

    Small per-turn block: the memories most similar in meaning to what the
    rider just said, so saddle memories surface for saddle questions
    regardless of age. Kept out of the cached blocks on purpose.
    """
    try:
        from app.services.memory_service import get_context

        block = get_context(db, user, limit=14, query=message)
        return block or None
    except Exception:
        logger.exception("Semantic recall failed for user %s", user.id)
        return None


def _dossier_block(db: Session, user: User) -> str:
    """The Rider Dossier + curiosity gaps, ready for the system prompt."""
    try:
        from app.services.dossier_service import dossier_context

        block = dossier_context(db, user.id)
        return block + "\n\n" if block else ""
    except Exception:
        logger.exception("Dossier context failed for user %s", user.id)
        return ""


def _build_rider_context(db: Session, user: User) -> str:
    """
    Build a comprehensive context snapshot of the rider's current state.

    This is injected into each message as context for the AI coach.
    Sections are ordered logically: who → wants → current state → history → plan → events.
    Each section is wrapped in try/except so a failure in one doesn't break the rest.
    """
    context: dict = {}
    today = date.today()

    # ── 1. Profile (enriched with physical data) ──
    profile: dict = {
        "name": user.full_name or "Rider",
        "ftp": user.ftp,
        "weight_kg": user.weight_kg,
        "experience": user.experience_level,
        "equipment": {
            "power_meter": user.has_power_meter,
            "smart_trainer": user.has_smart_trainer,
            "hr_monitor": user.has_hr_monitor,
        },
        "weekly_hours": user.weekly_hours_available,
    }
    if user.height_cm:
        profile["height_cm"] = user.height_cm
    if user.max_hr:
        profile["max_hr"] = user.max_hr
    if user.resting_hr:
        profile["resting_hr"] = user.resting_hr
    if user.date_of_birth:
        try:
            dob = user.date_of_birth
            if hasattr(dob, "date"):
                dob = dob.date()
            profile["age"] = today.year - dob.year - (
                (today.month, today.day) < (dob.month, dob.day)
            )
        except Exception:
            pass
    context["profile"] = profile

    # ── 2. Onboarding context (goals & motivation) ──
    try:
        onboarding = get_onboarding_response(db, user.id)
        if onboarding:
            ob: dict = {"primary_goal": onboarding.primary_goal}
            if onboarding.secondary_goals:
                ob["secondary_goals"] = onboarding.secondary_goals
            if onboarding.years_cycling:
                ob["years_cycling"] = onboarding.years_cycling
            if onboarding.indoor_outdoor_preference:
                ob["indoor_outdoor"] = onboarding.indoor_outdoor_preference
            context["onboarding"] = ob
    except Exception:
        pass

    # ── 3-5. Fitness + Power Profile + Profile Scores ──
    # Combined to avoid calling the expensive get_all_time_power_profile() twice.
    ftp = user.ftp or 0
    weight = user.weight_kg or 0

    # Tell the coach what's missing so zeros read as "not set up yet",
    # not "rider has no fitness". Drives FTP-test and Strava prompts.
    missing = []
    if not ftp:
        missing.append("ftp")
    if not weight:
        missing.append("weight")
    if missing:
        context["setup_incomplete"] = {
            "missing": missing,
            "note": (
                "This rider has not set these yet. Fitness numbers will look "
                "like zeros because there is nothing to compute from, not "
                "because they are unfit. Nudge them to set FTP (or ride an "
                "FTP test) and connect Strava before reading too much into "
                "the data."
            ),
        }

    try:
        fitness = get_current_fitness(db, user.id)

        # Power profile (expensive query — call once, reuse everywhere)
        power_profile_raw: dict = {}
        try:
            power_profile_raw = get_all_time_power_profile(db, user.id)
        except Exception:
            pass

        power_values = {d: v["best_power"] for d, v in power_profile_raw.items()}

        # Rider type profiling
        rider_profile = {"type": "unknown", "strengths": [], "weaknesses": []}
        if ftp > 0 and weight > 0:
            rider_profile = rider_type_profile(power_values, ftp, weight)

        # Profile scores (radar chart, 0-100 per energy system)
        profile_scores: dict = {}
        if weight > 0:
            profile_scores = rider_profile_scores(power_values, weight)

        # Fitness level classification
        ctl = fitness["ctl"]
        fitness_level = (
            "untrained" if ctl < 20 else
            "fair" if ctl < 40 else
            "moderate" if ctl < 60 else
            "good" if ctl < 80 else
            "very_good" if ctl < 100 else
            "excellent"
        )

        context["fitness"] = {
            "ctl": fitness["ctl"],
            "atl": fitness["atl"],
            "tsb": fitness["tsb"],
            "ramp_rate": fitness["ramp_rate"],
            "w_per_kg": round(calc_w_per_kg(ftp, weight), 2) if ftp and weight else None,
            "rider_type": rider_profile["type"],
            "strengths": rider_profile["strengths"],
            "weaknesses": rider_profile["weaknesses"],
            "fitness_level": fitness_level,
        }

        # Profile scores (Section H)
        if profile_scores:
            context["profile_scores"] = profile_scores

        # Power profile with best efforts (Section B)
        if power_profile_raw:
            duration_labels = {
                5: "5s", 10: "10s", 15: "15s", 30: "30s", 60: "1min",
                120: "2min", 300: "5min", 600: "10min", 1200: "20min",
                1800: "30min", 3600: "60min", 5400: "90min",
            }
            context["power_profile"] = {
                duration_labels.get(d, f"{d}s"): {
                    k: v for k, v in {
                        "watts": round(entry["best_power"]),
                        "w_per_kg": round(entry["best_power"] / weight, 2) if weight > 0 else None,
                        "date": str(entry["ride_date"]) if entry.get("ride_date") else None,
                    }.items() if v is not None
                }
                for d, entry in sorted(power_profile_raw.items())
                if entry["best_power"] > 0
            }

    except Exception:
        context["fitness"] = {"ctl": 0, "atl": 0, "tsb": 0}

    # ── 6. Power Zones ──
    try:
        zones = get_zones(user)
        if zones.get("power_zones"):
            context["power_zones"] = zones["power_zones"]
    except Exception:
        pass

    # ── 7. FTP History (progression over time) ──
    try:
        ftp_hist = get_ftp_history(db, user.id)
        if ftp_hist:
            context["ftp_history"] = [
                {"date": str(h["date"]), "ftp": h["ftp"]}
                for h in ftp_hist
            ]
    except Exception:
        pass

    # ── 8. Weekly Training Load (last 8 weeks) ──
    try:
        weekly = get_weekly_training_load(db, user.id, weeks=8)
        if weekly:
            context["weekly_load"] = [
                {
                    "week": str(w["week_start"]),
                    "tss": round(w["total_tss"]),
                    "rides": w["ride_count"],
                    "hours": round(w["total_duration_seconds"] / 3600, 1) if w["total_duration_seconds"] else 0,
                    "avg_if": w["avg_intensity_factor"],
                }
                for w in weekly
            ]
    except Exception:
        pass

    # ── 9. Training Compliance ──
    try:
        past_workouts = (
            db.query(Workout)
            .filter(
                Workout.user_id == user.id,
                Workout.scheduled_date <= today,
            )
            .all()
        )
        if past_workouts:
            total = len(past_workouts)
            status_counts = Counter(str(w.status) for w in past_workouts)
            completed = status_counts.get(WorkoutStatus.completed, 0) + status_counts.get("completed", 0)
            skipped = status_counts.get(WorkoutStatus.skipped, 0) + status_counts.get("skipped", 0)

            compliance_rate = round(completed / total * 100) if total > 0 else 0

            # Per-type compliance
            type_stats: dict = {}
            for w in past_workouts:
                wtype = str(w.workout_type)
                if wtype not in type_stats:
                    type_stats[wtype] = {"total": 0, "completed": 0}
                type_stats[wtype]["total"] += 1
                if str(w.status) in ("completed", "WorkoutStatus.completed"):
                    type_stats[wtype]["completed"] += 1

            type_compliance = {
                t: round(s["completed"] / s["total"] * 100)
                for t, s in type_stats.items()
                if s["total"] >= 2  # Only types with enough data
            }

            context["compliance"] = {
                "overall_pct": compliance_rate,
                "completed": completed,
                "skipped": skipped,
                "total_planned": total,
                "by_type": type_compliance,
            }
    except Exception:
        pass

    # ── 10. Training Plan + Current Phase ──
    try:
        plans = get_plans(db, user.id)
        active_plans = [p for p in plans if p.status == "active"]
        if active_plans:
            plan = active_plans[0]
            context["training_plan"] = {
                "name": plan.name,
                "start_date": str(plan.start_date),
                "end_date": str(plan.end_date),
                "model": plan.periodization_model,
            }

            # Current phase
            for phase in plan.phases:
                if phase.start_date <= today <= phase.end_date:
                    context["current_phase"] = {
                        "type": phase.phase_type,
                        "focus": phase.focus,
                        "start": str(phase.start_date),
                        "end": str(phase.end_date),
                    }
                    break
    except Exception:
        pass

    # ── 11. This Week's Workouts ──
    try:
        week_start = today - timedelta(days=today.weekday())  # Monday
        workouts = get_workouts_by_date(db, user.id, week_start=week_start)
        if workouts:
            context["this_week"] = [
                {
                    "id": w.id,
                    "date": str(w.scheduled_date),
                    "title": w.title,
                    "type": w.workout_type,
                    "description": w.description,
                    "status": w.status,
                    "planned_tss": w.planned_tss,
                    "planned_duration_min": round(w.planned_duration_seconds / 60) if w.planned_duration_seconds else None,
                }
                for w in workouts[:7]
            ]
    except Exception:
        pass

    # ── 12. Recent Rides (last 15) ──
    try:
        rides, _ = get_rides(db, user.id, page=1, per_page=15)
        if rides:
            context["recent_rides"] = [
                {
                    k: v for k, v in {
                        "date": str(r.ride_date.date() if hasattr(r.ride_date, "date") else r.ride_date),
                        "title": r.title,
                        "duration_min": round(r.duration_seconds / 60) if r.duration_seconds else None,
                        "tss": round(r.tss, 1) if r.tss else None,
                        "np": round(r.normalized_power) if r.normalized_power else None,
                        "if": round(r.intensity_factor, 2) if r.intensity_factor else None,
                        "distance_km": round(r.distance_meters / 1000, 1) if r.distance_meters else None,
                        "elevation_m": round(r.elevation_gain_meters) if r.elevation_gain_meters else None,
                        "avg_hr": r.average_hr,
                        "workout_id": r.workout_id,
                    }.items() if v is not None
                }
                for r in rides
            ]
    except Exception:
        pass

    # ── 13. Goal Events ──
    try:
        user_goals = get_goals(db, user.id)
        if user_goals:
            context["goal_events"] = []
            for g in user_goals:
                goal_info: dict = {
                    "event_name": g.event_name,
                    "event_date": str(g.event_date),
                    "event_type": g.event_type,
                    "priority": g.priority,
                }
                if g.event_date >= today:
                    goal_info["days_until"] = (g.event_date - today).days
                # Assessment data for completed goals
                if hasattr(g, "status") and g.status and g.status != "upcoming":
                    goal_info["status"] = g.status
                    if g.finish_time_seconds:
                        goal_info["finish_time_seconds"] = g.finish_time_seconds
                    if g.overall_satisfaction:
                        goal_info["overall_satisfaction"] = g.overall_satisfaction
                    if g.perceived_exertion:
                        goal_info["perceived_exertion"] = g.perceived_exertion
                    if g.assessment_data:
                        ad = g.assessment_data if isinstance(g.assessment_data, dict) else {}
                        if ad.get("went_well"):
                            goal_info["went_well"] = ad["went_well"]
                        if ad.get("to_improve"):
                            goal_info["to_improve"] = ad["to_improve"]
                    # Include actual ride metrics if linked
                    if g.actual_ride_id and hasattr(g, "actual_ride") and g.actual_ride:
                        ride = g.actual_ride
                        ride_info: dict = {}
                        if ride.normalized_power:
                            ride_info["np"] = round(ride.normalized_power)
                        if ride.intensity_factor:
                            ride_info["if"] = round(ride.intensity_factor, 2)
                        if ride.variability_index:
                            ride_info["vi"] = round(ride.variability_index, 2)
                        if ride.tss:
                            ride_info["tss"] = round(ride.tss, 1)
                        if ride_info:
                            goal_info["actual_ride_metrics"] = ride_info
                if g.target_duration_minutes:
                    goal_info["target_duration_minutes"] = g.target_duration_minutes
                if g.notes:
                    goal_info["notes"] = g.notes
                if g.route_url:
                    goal_info["route_url"] = g.route_url
                if g.route_data:
                    # Include route summary but exclude the full elevation profile
                    # (too large for coach context — hundreds of trackpoints)
                    rd = g.route_data if isinstance(g.route_data, dict) else {}
                    goal_info["route_data"] = {
                        k: v for k, v in rd.items()
                        if k != "elevation_profile"
                    }
                context["goal_events"].append(goal_info)
    except Exception:
        pass

    # ── 9. Long-term memory (the brain — Pillar 2) ──
    # Injected inside the context dict so the result stays valid JSON
    # (stream_response round-trips this via json.loads for the snapshot).
    try:
        from app.services.memory_service import get_context as _memory_context

        memory_block = _memory_context(db, user)
        if memory_block:
            context["long_term_memory"] = memory_block.split("\n")
    except Exception:
        import logging

        logging.getLogger(__name__).exception("Memory context failed (user=%s)", user.id)

    return json.dumps(context, indent=2, default=str)


# === Coach Tools (Claude tool_use) ===

COACH_TOOLS = [
    {
        "name": "update_workout",
        "description": "Update an existing workout's title, description, type, date, duration, or TSS. Use this when the rider and coach agree to modify a planned workout — e.g. changing a threshold session to an endurance ride, adjusting duration, or rewriting the description.",
        "input_schema": {
            "type": "object",
            "properties": {
                "workout_id": {
                    "type": "string",
                    "description": "The ID of the workout to update (from this_week context)",
                },
                "title": {"type": "string", "description": "New workout title"},
                "description": {"type": "string", "description": "New workout description explaining purpose and how to perform it"},
                "workout_type": {
                    "type": "string",
                    "enum": ["endurance", "tempo", "sweet_spot", "threshold", "vo2max", "sprint", "recovery", "rest"],
                    "description": "New workout type",
                },
                "scheduled_date": {"type": "string", "description": "New date in YYYY-MM-DD format"},
                "planned_duration_seconds": {"type": "integer", "description": "New planned duration in seconds"},
                "planned_tss": {"type": "number", "description": "New planned TSS"},
            },
            "required": ["workout_id"],
        },
    },
    {
        "name": "swap_workout_date",
        "description": "Swap the scheduled dates of two workouts. Use when the rider wants to rearrange their week — e.g. moving Tuesday's intervals to Thursday.",
        "input_schema": {
            "type": "object",
            "properties": {
                "workout_id_a": {"type": "string", "description": "First workout ID"},
                "workout_id_b": {"type": "string", "description": "Second workout ID"},
            },
            "required": ["workout_id_a", "workout_id_b"],
        },
    },
    {
        "name": "add_workout",
        "description": "Add a new workout to the rider's plan. Use when the coach prescribes an additional session — e.g. adding a recovery ride or an extra interval session.",
        "input_schema": {
            "type": "object",
            "properties": {
                "scheduled_date": {"type": "string", "description": "Date in YYYY-MM-DD format"},
                "title": {"type": "string", "description": "Workout title"},
                "description": {"type": "string", "description": "Description of purpose and how to perform the workout"},
                "workout_type": {
                    "type": "string",
                    "enum": ["endurance", "tempo", "sweet_spot", "threshold", "vo2max", "sprint", "recovery", "rest"],
                },
                "planned_duration_seconds": {"type": "integer", "description": "Duration in seconds"},
                "planned_tss": {"type": "number", "description": "Estimated TSS"},
            },
            "required": ["scheduled_date", "title", "workout_type"],
        },
    },
    {
        "name": "skip_workout",
        "description": "Mark a workout as skipped. Use when the rider and coach agree to drop a session — due to fatigue, time constraints, or plan adjustment.",
        "input_schema": {
            "type": "object",
            "properties": {
                "workout_id": {"type": "string", "description": "The workout ID to skip"},
            },
            "required": ["workout_id"],
        },
    },
]


def _execute_tool(db: Session, user: User, tool_name: str, tool_input: dict) -> str:
    """
    Execute a coach tool and return a result string for Claude.

    Each tool modifies the training plan in the database and returns
    a confirmation message that Claude uses in its follow-up response.
    """
    if tool_name == "update_workout":
        workout = (
            db.query(Workout)
            .filter(Workout.id == tool_input["workout_id"], Workout.user_id == user.id)
            .first()
        )
        if not workout:
            return "Error: Workout not found."

        if "title" in tool_input:
            workout.title = tool_input["title"]
        if "description" in tool_input:
            workout.description = tool_input["description"]
        if "workout_type" in tool_input:
            workout.workout_type = tool_input["workout_type"]
        if "scheduled_date" in tool_input:
            workout.scheduled_date = date.fromisoformat(tool_input["scheduled_date"])
        if "planned_duration_seconds" in tool_input:
            workout.planned_duration_seconds = tool_input["planned_duration_seconds"]
        if "planned_tss" in tool_input:
            workout.planned_tss = tool_input["planned_tss"]

        workout.status = WorkoutStatus.modified
        db.commit()
        return f"Updated workout '{workout.title}' on {workout.scheduled_date}."

    elif tool_name == "swap_workout_date":
        wa = (
            db.query(Workout)
            .filter(Workout.id == tool_input["workout_id_a"], Workout.user_id == user.id)
            .first()
        )
        wb = (
            db.query(Workout)
            .filter(Workout.id == tool_input["workout_id_b"], Workout.user_id == user.id)
            .first()
        )
        if not wa or not wb:
            return "Error: One or both workouts not found."

        wa.scheduled_date, wb.scheduled_date = wb.scheduled_date, wa.scheduled_date
        db.commit()
        return f"Swapped dates: '{wa.title}' now on {wa.scheduled_date}, '{wb.title}' now on {wb.scheduled_date}."

    elif tool_name == "add_workout":
        workout = Workout(
            user_id=user.id,
            scheduled_date=date.fromisoformat(tool_input["scheduled_date"]),
            title=tool_input["title"],
            description=tool_input.get("description"),
            workout_type=tool_input["workout_type"],
            planned_duration_seconds=tool_input.get("planned_duration_seconds"),
            planned_tss=tool_input.get("planned_tss"),
            status=WorkoutStatus.planned,
        )
        db.add(workout)
        db.commit()
        db.refresh(workout)
        return f"Added workout '{workout.title}' on {workout.scheduled_date} (ID: {workout.id})."

    elif tool_name == "skip_workout":
        workout = (
            db.query(Workout)
            .filter(Workout.id == tool_input["workout_id"], Workout.user_id == user.id)
            .first()
        )
        if not workout:
            return "Error: Workout not found."

        workout.status = WorkoutStatus.skipped
        db.commit()
        return f"Skipped workout '{workout.title}' on {workout.scheduled_date}."

    return f"Error: Unknown tool '{tool_name}'."


# === Chat Session Management ===

def create_session(db: Session, user_id: str, title: str | None = None) -> ChatSession:
    """Create a new chat session."""
    session = ChatSession(
        user_id=user_id,
        title=title or f"Chat - {datetime.now(timezone.utc).strftime('%d %b %Y')}",
    )
    db.add(session)
    db.commit()
    db.refresh(session)
    return session


def get_sessions(
    db: Session, user_id: str, include_archived: bool = False
) -> list[ChatSession]:
    """Get chat sessions for a user. Pinned first, then newest."""
    q = db.query(ChatSession).filter(ChatSession.user_id == user_id)
    if not include_archived:
        q = q.filter(ChatSession.archived_at.is_(None))
    return (
        q.order_by(ChatSession.pinned.desc(), ChatSession.created_at.desc())
        .all()
    )


def update_session(
    db: Session,
    session: ChatSession,
    *,
    title: str | None = None,
    pinned: bool | None = None,
    starred: bool | None = None,
    archived: bool | None = None,
) -> ChatSession:
    """Update session management fields. Only passed fields change."""
    if title is not None:
        session.title = title.strip()[:255] or session.title
    if pinned is not None:
        session.pinned = pinned
    if starred is not None:
        session.starred = starred
    if archived is not None:
        session.archived_at = datetime.now(timezone.utc) if archived else None
        if archived:
            session.pinned = False  # archived chats don't hold a pin
    db.commit()
    db.refresh(session)
    return session


def delete_session(db: Session, session: ChatSession) -> None:
    """Hard-delete a session and its messages (cascade)."""
    db.delete(session)
    db.commit()


def get_session(db: Session, session_id: str, user_id: str) -> ChatSession | None:
    """Get a single chat session with messages."""
    return (
        db.query(ChatSession)
        .filter(ChatSession.id == session_id, ChatSession.user_id == user_id)
        .first()
    )


def add_user_message(db: Session, session: ChatSession, content: str) -> ChatMessage:
    """Add a user message to a session."""
    message = ChatMessage(
        session_id=session.id,
        role=ChatRole.user,
        content=content,
    )
    db.add(message)
    db.commit()
    db.refresh(message)
    return message


def add_assistant_message(
    db: Session, session: ChatSession, content: str,
    context_snapshot: dict | None = None, tokens_used: int | None = None,
) -> ChatMessage:
    """Add an assistant message to a session."""
    message = ChatMessage(
        session_id=session.id,
        role=ChatRole.assistant,
        content=content,
        context_snapshot=context_snapshot,
        tokens_used=tokens_used,
    )
    db.add(message)
    db.commit()
    db.refresh(message)
    return message


# === Claude API Integration ===

def _build_messages(session: ChatSession, max_messages: int = 20) -> list[dict]:
    """Build messages list for Claude API from chat history."""
    messages = sorted(session.messages, key=lambda m: m.created_at)

    # Take last N messages
    recent = messages[-max_messages:] if len(messages) > max_messages else messages

    return [
        {"role": msg.role, "content": msg.content}
        for msg in recent
    ]


async def stream_response(
    db: Session, user: User, session: ChatSession, user_message: str
):
    """
    Send message to Claude and stream response back with tool use support.

    Implements an agentic loop: when Claude calls a tool, we execute it,
    send the result back, and let Claude continue streaming its follow-up.

    Yields SSE-formatted chunks:
        data: {"type": "text", "content": "..."}
        data: {"type": "plan_updated"}   -- signals frontend to refresh training data
        data: {"type": "done"}
    """
    # Save user message
    add_user_message(db, session, user_message)

    # Build context
    rider_context = _build_rider_context(db, user)
    dossier_block = _dossier_block(db, user)

    # Build system prompt with rider context + per-message semantic recall
    system = _system_blocks(
        user,
        f"## Current Rider Context\n```json\n{rider_context}\n```\n\n"
        f"{dossier_block}"
        f"Today's date: {date.today().isoformat()}",
        volatile=_relevant_memories(db, user, user_message),
    )

    # Build message history
    messages = _build_messages(session)

    # Stream from Claude with agentic tool loop — via the forma-core funnel
    full_response = ""
    tokens_used = 0
    plan_was_updated = False
    scrub = StreamHumanizer()

    try:
        # Agentic loop — keeps going while Claude wants to call tools
        max_iterations = 5
        for _ in range(max_iterations):
            with forma_core.stream(
                user_id=user.id,
                task="chat",
                surface="coach",
                system=system,
                messages=messages,
                tools=COACH_TOOLS,
            ) as stream:
                for event in stream:
                    if event.type == "content_block_delta":
                        if hasattr(event.delta, "text"):
                            clean = scrub.feed(event.delta.text)
                            if clean:
                                full_response += clean
                                yield f'data: {json.dumps({"type": "text", "content": clean})}\n\n'

                final = stream.get_final_message()
                tokens_used += (
                    final.usage.input_tokens + final.usage.output_tokens
                    if final.usage else 0
                )

            # Check if Claude wants to use tools
            tool_use_blocks = [
                block for block in final.content
                if block.type == "tool_use"
            ]

            if not tool_use_blocks or final.stop_reason != "tool_use":
                # No tool calls — we're done
                break

            # Execute tool calls and build tool_result messages
            # Append assistant message with all content blocks
            messages.append({"role": "assistant", "content": final.content})

            tool_results = []
            for tool_block in tool_use_blocks:
                result_text = _execute_tool(
                    db, user, tool_block.name, tool_block.input
                )
                tool_results.append({
                    "type": "tool_result",
                    "tool_use_id": tool_block.id,
                    "content": result_text,
                })
                plan_was_updated = True

            messages.append({"role": "user", "content": tool_results})

            # Signal frontend that training plan was modified
            yield f'data: {json.dumps({"type": "plan_updated"})}\n\n'

            # Loop continues — Claude will respond to the tool results

        tail = scrub.flush()
        if tail:
            full_response += tail
            yield f'data: {json.dumps({"type": "text", "content": tail})}\n\n'

    except forma_core.BudgetExceededError:
        full_response = forma_core.QUOTA_MESSAGE
        yield f'data: {json.dumps({"type": "text", "content": full_response})}\n\n'
    except Exception as e:
        # ANY failure — provider error, timeout, tool bug — must never leave
        # the rider staring at an empty bubble. Log the real cause; the rider
        # gets something honest and human.
        logger.exception("Coach chat stream failed: %s", e)
        error_msg = "That one didn't reach me. Give it a second and send it again."
        # Don't double up if some text already streamed before the failure.
        if not full_response.strip():
            full_response = error_msg
            yield f'data: {json.dumps({"type": "text", "content": error_msg})}\n\n'

    # The model can spend its whole token budget before any prose reaches
    # the rider (a truncated tool call streams zero text and raises nothing).
    # A rider's message must NEVER sit unanswered in the history.
    if not full_response.strip():
        full_response = (
            "I got cut off before I could answer that properly. "
            "Send it again and I'll get straight to the point."
        )
        yield f'data: {json.dumps({"type": "text", "content": full_response})}\n\n'

    context_snapshot = json.loads(rider_context) if rider_context else None
    add_assistant_message(db, session, full_response, context_snapshot, tokens_used)

    # Final plan_updated signal if tools were used (in case frontend missed it)
    if plan_was_updated:
        yield f'data: {json.dumps({"type": "plan_updated"})}\n\n'

    yield f'data: {json.dumps({"type": "done"})}\n\n'

    # Memory extraction — write this exchange into the brain (Pillar 2).
    # Runs after the client has received `done`, so it never delays the stream.
    try:
        from app.services.memory_service import extract_memories

        extract_memories(
            db,
            user,
            f"Rider: {user_message}\n\nForma: {full_response}",
            source="chat",
            source_ref=session.id,
        )
    except Exception:
        import logging

        logging.getLogger(__name__).exception(
            "Memory extraction after chat failed (user=%s)", user.id
        )

    # Name the thread from its content while it still wears the default name.
    maybe_autotitle_session(db, user, session)


VOICE_MODE_ADDENDUM = """
## Voice Mode Instructions
You are speaking out loud to the rider. Adjust your style:
- Keep responses conversational and concise — aim for 3-5 sentences unless they ask for detail
- Avoid markdown formatting, bullet points, numbered lists, and code blocks
- Use natural spoken language with contractions ("you're", "don't", "let's")
- Keep sentences short and clear — they will be read aloud
- Don't use special characters like asterisks, hashtags, or brackets
- Use "about" instead of precise decimals when speaking numbers
- It's fine to be warm and casual — you're having a conversation, not writing an essay
"""

# Regex for detecting sentence boundaries in streamed text
_SENTENCE_END = re.compile(r'[.!?]\s+|[.!?]$')

# The default name a session is born with ("Chat - 24 Jul 2026").
_DEFAULT_TITLE_RE = re.compile(r"^Chat( - |$)")


def maybe_autotitle_session(db: Session, user: User, session: ChatSession) -> None:
    """Name the thread from its content — only while it wears the default name.

    A title the rider typed (or previously auto-generated) is never touched.
    Cheap Haiku call, runs after the reply has already streamed.
    """
    try:
        if session.title and not _DEFAULT_TITLE_RE.match(session.title):
            return
        msgs = sorted(session.messages, key=lambda m: m.created_at)
        if len(msgs) < 2:
            return
        sample = "\n".join(f"{m.role}: {m.content[:300]}" for m in msgs[:6])
        resp = forma_core.call(
            user_id=user.id,
            task="chat_title",
            surface="coach",
            system=(
                "Name this cycling-coach conversation in 2-5 words for a sidebar. "
                "Specific and plain, sentence case, no quotes, no trailing "
                "punctuation, no emoji. Examples: Fuelling for the 312 · "
                "Tuesday intervals rethink · Saddle pain fix"
            ),
            messages=[{"role": "user", "content": sample}],
        )
        title = response_text(resp).strip().strip('"').strip()
        if title:
            session.title = title[:255]
            db.commit()
    except Exception:
        logger.exception("Auto-title failed for session %s", session.id)


async def stream_voice_response(
    db: Session, user: User, session: ChatSession, user_message: str
):
    """
    Stream both text and audio responses via SSE with tool use support.

    Pipeline:
    1. Stream text from Claude (with agentic tool loop)
    2. Accumulate into sentences
    3. For each complete sentence, convert to audio via ElevenLabs
    4. Yield both text chunks and base64-encoded audio chunks

    SSE event types:
        data: {"type": "text", "content": "..."}
        data: {"type": "audio", "content": "<base64>", "sentence_index": N}
        data: {"type": "plan_updated"}
        data: {"type": "done"}

    Gracefully degrades — if ElevenLabs fails, text still streams normally.
    """
    from app.services.voice_service import is_voice_enabled, text_to_speech

    # Save user message
    add_user_message(db, session, user_message)

    # Build context
    rider_context = _build_rider_context(db, user)
    dossier_block = _dossier_block(db, user)

    # Build system prompt with voice mode addendum + per-message recall
    system = _system_blocks(
        user,
        f"{VOICE_MODE_ADDENDUM}\n\n"
        f"## Current Rider Context\n```json\n{rider_context}\n```\n\n"
        f"{dossier_block}"
        f"Today's date: {date.today().isoformat()}",
        volatile=_relevant_memories(db, user, user_message),
    )

    # Build message history
    messages = _build_messages(session)

    # Stream from Claude — via the forma-core funnel
    full_response = ""
    sentence_buffer = ""
    sentence_index = 0
    tokens_used = 0
    voice_enabled = is_voice_enabled()
    plan_was_updated = False
    scrub = StreamHumanizer()

    try:
        # Agentic loop — keeps going while Claude wants to call tools
        max_iterations = 5
        for _ in range(max_iterations):
            with forma_core.stream(
                user_id=user.id,
                task="chat_voice",  # shorter max_tokens — conciseness matters
                surface="coach_voice",
                system=system,
                messages=messages,
                tools=COACH_TOOLS,
            ) as stream:
                for event in stream:
                    if event.type == "content_block_delta":
                        if hasattr(event.delta, "text"):
                            text = scrub.feed(event.delta.text)
                            if not text:
                                continue
                            full_response += text
                            sentence_buffer += text

                            # Yield text chunk
                            yield f'data: {json.dumps({"type": "text", "content": text})}\n\n'

                            # Check for complete sentences and convert to audio
                            if voice_enabled:
                                while _SENTENCE_END.search(sentence_buffer):
                                    match = _SENTENCE_END.search(sentence_buffer)
                                    end_pos = match.end()
                                    complete_sentence = sentence_buffer[:end_pos].strip()
                                    sentence_buffer = sentence_buffer[end_pos:]

                                    if complete_sentence and len(complete_sentence) > 5:
                                        try:
                                            audio_bytes = await text_to_speech(
                                                complete_sentence
                                            )
                                            audio_b64 = base64.b64encode(
                                                audio_bytes
                                            ).decode("utf-8")
                                            yield f'data: {json.dumps({"type": "audio", "content": audio_b64, "sentence_index": sentence_index})}\n\n'
                                            sentence_index += 1
                                        except Exception:
                                            pass

                final = stream.get_final_message()
                tokens_used += (
                    final.usage.input_tokens + final.usage.output_tokens
                    if final.usage else 0
                )

            # Check if Claude wants to use tools
            tool_use_blocks = [
                block for block in final.content
                if block.type == "tool_use"
            ]

            if not tool_use_blocks or final.stop_reason != "tool_use":
                break

            # Execute tool calls
            messages.append({"role": "assistant", "content": final.content})

            tool_results = []
            for tool_block in tool_use_blocks:
                result_text = _execute_tool(
                    db, user, tool_block.name, tool_block.input
                )
                tool_results.append({
                    "type": "tool_result",
                    "tool_use_id": tool_block.id,
                    "content": result_text,
                })
                plan_was_updated = True

            messages.append({"role": "user", "content": tool_results})
            yield f'data: {json.dumps({"type": "plan_updated"})}\n\n'

        tail = scrub.flush()
        if tail:
            full_response += tail
            sentence_buffer += tail
            yield f'data: {json.dumps({"type": "text", "content": tail})}\n\n'

        # Handle any remaining text in buffer
        if voice_enabled and sentence_buffer.strip() and len(
            sentence_buffer.strip()
        ) > 5:
            try:
                audio_bytes = await text_to_speech(
                    sentence_buffer.strip()
                )
                audio_b64 = base64.b64encode(audio_bytes).decode("utf-8")
                yield f'data: {json.dumps({"type": "audio", "content": audio_b64, "sentence_index": sentence_index})}\n\n'
            except Exception as tts_err:
                # Degrade to text-only but say so in the logs — a dead
                # ElevenLabs key should never be an invisible failure.
                logger.warning("TTS failed (text continues): %s", tts_err)

    except forma_core.BudgetExceededError:
        full_response = forma_core.QUOTA_MESSAGE
        yield f'data: {json.dumps({"type": "text", "content": full_response})}\n\n'
    except Exception as e:
        # Never leave the rider with silence AND an empty bubble.
        logger.exception("Coach voice stream failed: %s", e)
        error_msg = "That one didn't reach me. Give it a second and send it again."
        if not full_response.strip():
            full_response = error_msg
            yield f'data: {json.dumps({"type": "text", "content": error_msg})}\n\n'

    # A rider's message must NEVER sit unanswered in the history (the
    # truncated-tool-call case streams zero text and raises nothing).
    if not full_response.strip():
        full_response = (
            "I got cut off before I could answer that properly. "
            "Send it again and I'll get straight to the point."
        )
        yield f'data: {json.dumps({"type": "text", "content": full_response})}\n\n'

    context_snapshot = json.loads(rider_context) if rider_context else None
    add_assistant_message(db, session, full_response, context_snapshot, tokens_used)

    if plan_was_updated:
        yield f'data: {json.dumps({"type": "plan_updated"})}\n\n'

    yield f'data: {json.dumps({"type": "done"})}\n\n'

    # Memory extraction — voice conversations feed the brain too (Pillar 2).
    try:
        from app.services.memory_service import extract_memories

        extract_memories(
            db, user,
            f"Rider: {user_message}\n\nForma: {full_response}",
            source="chat", source_ref=session.id,
        )
    except Exception:
        import logging

        logging.getLogger(__name__).exception(
            "Memory extraction after voice chat failed (user=%s)", user.id
        )

    # Name the thread from its content while it still wears the default name.
    maybe_autotitle_session(db, user, session)


def get_non_streaming_response(
    db: Session, user: User, session: ChatSession, user_message: str
) -> str:
    """
    Non-streaming version for simpler integrations.
    Returns the full response text.
    """
    add_user_message(db, session, user_message)

    rider_context = _build_rider_context(db, user)

    system = _system_blocks(
        user,
        f"## Current Rider Context\n```json\n{rider_context}\n```\n\n"
        f"Today's date: {date.today().isoformat()}"
    )

    messages = _build_messages(session)

    try:
        response = forma_core.call(
            user_id=user.id,
            task="chat_sync",
            surface="coach",
            system=system,
            messages=messages,
        )
    except forma_core.BudgetExceededError:
        add_assistant_message(db, session, forma_core.QUOTA_MESSAGE, None, 0)
        return forma_core.QUOTA_MESSAGE

    content = humanize(response_text(response))
    tokens_used = (
        response.usage.input_tokens + response.usage.output_tokens
        if response.usage else 0
    )

    context_snapshot = json.loads(rider_context) if rider_context else None
    add_assistant_message(db, session, content, context_snapshot, tokens_used)

    # Memory extraction — every conversational surface writes to the brain.
    try:
        from app.services.memory_service import extract_memories

        extract_memories(
            db, user,
            f"Rider: {user_message}\n\nForma: {content}",
            source="chat", source_ref=session.id,
        )
    except Exception:
        import logging

        logging.getLogger(__name__).exception(
            "Memory extraction after non-streaming chat failed (user=%s)", user.id
        )

    return content
