"""Read-only: sessions the coach booked outside any plan, per rider.

Until 16 Oct 2026 these were invisible: the calendar only showed sessions
inside an active plan, and the coach's add_workout never set a phase. The
deploy that fixes it makes all of them appear at once, so this shows what
will appear before anyone is surprised by it. Changes nothing.

    python -m scripts.report_coach_bookings
"""

from collections import defaultdict
from datetime import date

from app.database import SessionLocal
from app.models.training import Workout


def main() -> None:
    db = SessionLocal()
    try:
        today = date.today()
        rows = (
            db.query(Workout.user_id, Workout.scheduled_date, Workout.status)
            .filter(Workout.phase_id.is_(None))
            .all()
        )
        by_rider: dict[str, list] = defaultdict(list)
        for user_id, when, status in rows:
            by_rider[user_id].append((when, getattr(status, "value", status)))

        print(f"{len(rows)} sessions outside any plan, {len(by_rider)} riders")
        for user_id, items in sorted(by_rider.items(), key=lambda kv: -len(kv[1])):
            dates = sorted(d for d, _ in items)
            ahead = sum(1 for d, s in items if d >= today and s == "planned")
            past_planned = sum(1 for d, s in items if d < today and s == "planned")
            print(
                f"rider {user_id[:8]}: {len(items)} in all, {ahead} still ahead, "
                f"{past_planned} past and never ridden or skipped, "
                f"{dates[0]} to {dates[-1]}"
            )
    finally:
        db.close()


if __name__ == "__main__":
    main()
