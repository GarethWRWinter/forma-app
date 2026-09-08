"""The launch door: one word, one code, a cap.

    ... scripts/invite_codes.py mint HUNDRED --max-uses 100 --expires 2026-10-15 --note "Founding hundred"
    ... scripts/invite_codes.py list
    ... scripts/invite_codes.py revoke HUNDRED

Run with the production database exactly as the other scripts:
    railway run --service Postgres bash -c 'DATABASE_URL="$DATABASE_PUBLIC_URL" .venv/bin/python scripts/invite_codes.py list'

The register page pre-fills a code from a link: app.ridewithforma.com/register?invite=HUNDRED
"""

import argparse
from datetime import datetime

from app.database import SessionLocal
from app.models.invite import InviteCode


def main() -> None:
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    m = sub.add_parser("mint")
    m.add_argument("code", help="The word riders type, e.g. HUNDRED")
    m.add_argument("--max-uses", type=int, default=100)
    m.add_argument("--expires", help="YYYY-MM-DD (optional)")
    m.add_argument("--note", default="")
    sub.add_parser("list")
    r = sub.add_parser("revoke")
    r.add_argument("code")
    args = ap.parse_args()

    db = SessionLocal()
    try:
        if args.cmd == "mint":
            code = args.code.strip().upper()
            if db.query(InviteCode).filter(InviteCode.code == code).first():
                raise SystemExit(f"{code} already exists. Use list, or revoke it first.")
            row = InviteCode(
                code=code, max_uses=args.max_uses, uses=0, note=args.note or None,
                expires_at=datetime.fromisoformat(args.expires) if args.expires else None,
            )
            db.add(row)
            db.commit()
            print(f"Minted {code}: {args.max_uses} uses, expires {args.expires or 'never'}.")
            print(f"Link: https://app.ridewithforma.com/register?invite={code}")
        elif args.cmd == "list":
            rows = db.query(InviteCode).order_by(InviteCode.created_at).all()
            if not rows:
                print("No invite codes exist. The door is shut.")
            for c in rows:
                live = c.uses < c.max_uses and (c.expires_at is None or c.expires_at > datetime.utcnow())
                print(f"{c.code:<12} {c.uses}/{c.max_uses} used  expires {c.expires_at or 'never'}  {'LIVE' if live else 'closed'}  {c.note or ''}")
        elif args.cmd == "revoke":
            code = args.code.strip().upper()
            c = db.query(InviteCode).filter(InviteCode.code == code).first()
            if c is None:
                raise SystemExit(f"No such code {code}")
            c.max_uses = c.uses  # nothing left to redeem; history kept
            db.commit()
            print(f"Revoked {code}. {c.uses} rider(s) had used it; that stands.")
    finally:
        db.close()


if __name__ == "__main__":
    main()
