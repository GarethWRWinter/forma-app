"""Re-cost every Forma call from its stored token counts at today's prices.

Run after a change to _PRICES_PER_MTOK in app/core/forma_core.py, so the
monthly spend cap and the admin page count what Anthropic actually bills.
Until 21 Oct 2026 Sonnet 5 was priced at $3/$15 per million tokens instead
of its list $2/$10, which stopped riders at about two thirds of their cap.

Dry run by default: prints what would change. Pass --apply to write.
Running it twice changes nothing the second time.

Usage:  python -m scripts.reprice_forma_calls [--apply]
"""
import argparse
from collections import defaultdict
from datetime import datetime
from types import SimpleNamespace

from app.core.forma_core import cost_cents
from app.database import SessionLocal
from app.models.forma_call import FormaCall


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="write the new costs (default: dry run)")
    args = ap.parse_args()
    db = SessionLocal()
    try:
        month_start = datetime.utcnow().replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        changed = 0
        before = after = 0.0
        month = defaultdict(lambda: [0.0, 0.0])
        for call in db.query(FormaCall).yield_per(500):
            usage = SimpleNamespace(
                input_tokens=call.input_tokens,
                output_tokens=call.output_tokens,
                cache_read_input_tokens=call.cache_read_tokens,
                cache_creation_input_tokens=call.cache_write_tokens,
            )
            new = cost_cents(call.model, usage)
            before += call.cost_cents or 0.0
            after += new
            if call.ts >= month_start:
                month[call.user_id][0] += call.cost_cents or 0.0
                month[call.user_id][1] += new
            if abs(new - (call.cost_cents or 0.0)) > 1e-9:
                changed += 1
                if args.apply:
                    call.cost_cents = new
        if args.apply:
            db.commit()

        print(f"{changed} calls re-costed; all time ${before / 100:.2f} -> ${after / 100:.2f}")
        for user_id, (b, a) in sorted(month.items(), key=lambda kv: -kv[1][0]):
            print(f"rider {user_id[:8]} this month: ${b / 100:.2f} -> ${a / 100:.2f}")
        print("Written." if args.apply else "Dry run: nothing written. Pass --apply to write.")
    finally:
        db.close()


if __name__ == "__main__":
    main()
