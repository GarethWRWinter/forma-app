"""scripts/backfill_subscription_cancel_at.py: riders who cancelled before
users.subscription_cancel_at existed get their end date, read from Stripe
(faked here), the way the webhook would have set it."""

from datetime import datetime, timezone

import pytest

from app.config import settings
from app.models.user import User
from app.services import billing_service
from scripts import backfill_subscription_cancel_at as backfill

END = 1_800_000_000
DOWN = "customers Stripe fails on"


def _naive(ts: int) -> datetime:
    return datetime.fromtimestamp(ts, tz=timezone.utc).replace(tzinfo=None)


class _Obj(dict):
    def to_dict(self):
        return dict(self)


class _Page:
    def __init__(self, items):
        self.items = items

    def auto_paging_iter(self):
        return iter(list(self.items))


def _sub(sid, status="active", **kw):
    sub = _Obj(
        id=sid, status=status, cancel_at=None, cancel_at_period_end=False,
        items={"object": "list", "data": [{"id": "si_" + sid, "current_period_end": END}]},
    )
    sub.update(kw)
    return sub


@pytest.fixture
def stripe_subs(monkeypatch):
    """Subscriptions by customer id. Listing a customer in DOWN fails."""
    subs: dict[str, list] = {}
    monkeypatch.setattr(settings, "stripe_secret_key", "sk_test_forma")
    monkeypatch.setattr(settings, "stripe_price_id", "price_forma")

    def _list(customer=None, status=None, limit=None):
        assert status == "all"
        if customer in subs.get(DOWN, ()):
            raise RuntimeError("No such customer")
        return _Page(subs.get(customer, []))

    def _never(*a, **k):
        raise AssertionError("the backfill only reads from Stripe")

    monkeypatch.setattr(billing_service.stripe.Subscription, "list", _list)
    monkeypatch.setattr(billing_service.stripe.Subscription, "modify", _never)
    monkeypatch.setattr(billing_service.stripe.Subscription, "cancel", _never)
    return subs


def _rider(db, email, customer, status="active", cancel_at=None):
    user = User(email=email, hashed_password="x", stripe_customer_id=customer,
                subscription_status=status, subscription_period_end=_naive(END),
                subscription_cancel_at=cancel_at)
    db.add(user)
    db.commit()
    return user


def _run(db, *argv):
    return backfill.run(db, backfill.parse_args(list(argv)))


def test_a_rider_who_cancelled_before_the_deploy_gets_the_date(db_session, stripe_subs):
    portal = _rider(db_session, "portal@example.com", "cus_portal")
    legacy = _rider(db_session, "legacy@example.com", "cus_legacy")
    renews = _rider(db_session, "renews@example.com", "cus_renews")
    later = _rider(db_session, "later@example.com", "cus_later")
    stripe_subs["cus_portal"] = [_sub("p", cancel_at=END)]
    stripe_subs["cus_legacy"] = [_sub("l", cancel_at_period_end=True)]
    stripe_subs["cus_renews"] = [_sub("r")]
    # A cancel date in a later period renews once more: no date yet.
    stripe_subs["cus_later"] = [_sub("x", cancel_at=END + 86_400 * 30)]

    code, lines = _run(db_session, "--commit")
    assert code == 0, lines
    for user in (portal, legacy, renews, later):
        db_session.refresh(user)
    assert portal.subscription_cancel_at == _naive(END)
    assert legacy.subscription_cancel_at == _naive(END)
    assert renews.subscription_cancel_at is None
    assert later.subscription_cancel_at is None
    # Nothing else on the row moves.
    assert portal.subscription_status == "active"
    assert portal.subscription_period_end == _naive(END)
    text = "\n".join(lines)
    assert "Stripe mode: test" in text
    assert "portal@example.com  cus_portal  ends: not set becomes " in text
    assert "renews@example.com" not in text  # unchanged, so not listed
    assert text.endswith("Changed 2, unchanged 2, couldn't read 0.")


def test_the_dry_run_writes_nothing(db_session, stripe_subs):
    portal = _rider(db_session, "portal@example.com", "cus_portal")
    stripe_subs["cus_portal"] = [_sub("p", cancel_at=END)]

    code, lines = _run(db_session)
    assert code == 0
    db_session.expire_all()
    assert db_session.get(User, portal.id).subscription_cancel_at is None
    assert lines[-1] == "Would change 1, unchanged 0, couldn't read 0."


def test_a_stale_date_is_cleared_and_a_second_run_changes_nothing(db_session, stripe_subs):
    renewed = _rider(db_session, "renewed@example.com", "cus_r", cancel_at=_naive(END))
    stripe_subs["cus_r"] = [_sub("r")]  # they pressed Renew

    assert _run(db_session, "--commit")[0] == 0
    db_session.refresh(renewed)
    assert renewed.subscription_cancel_at is None
    code, lines = _run(db_session, "--commit")
    assert code == 0 and lines[-1] == "Changed 0, unchanged 1, couldn't read 0."


def test_several_subscriptions(db_session, stripe_subs):
    both_end = _rider(db_session, "two@example.com", "cus_two")
    one_renews = _rider(db_session, "mixed@example.com", "cus_mixed")
    stripe_subs["cus_two"] = [
        _sub("a", cancel_at=END - 86_400),
        _sub("b", cancel_at=END),
        _sub("old", status="canceled", cancel_at=END - 86_400 * 90),
    ]
    stripe_subs["cus_mixed"] = [_sub("c", cancel_at=END), _sub("d")]

    assert _run(db_session, "--commit")[0] == 0
    db_session.refresh(both_end)
    db_session.refresh(one_renews)
    assert both_end.subscription_cancel_at == _naive(END)  # the last to end
    assert one_renews.subscription_cancel_at is None  # still charged at renewal


def test_only_riders_stripe_could_renew_are_read(db_session, stripe_subs):
    gone = _rider(db_session, "gone@example.com", "cus_gone", status="canceled")
    _rider(db_session, "nobody@example.com", None, status="none")
    stripe_subs["cus_gone"] = [_sub("g", cancel_at=END)]

    code, lines = _run(db_session, "--commit")
    assert code == 0
    assert lines[0].endswith("could renew: 0")
    db_session.refresh(gone)
    assert gone.subscription_cancel_at is None


def test_no_live_subscription_in_stripe_is_left_alone(db_session, stripe_subs):
    stale = _rider(db_session, "stale@example.com", "cus_stale", cancel_at=_naive(END))
    stripe_subs["cus_stale"] = [_sub("s", status="canceled")]

    code, lines = _run(db_session, "--commit")
    assert code == 0
    db_session.refresh(stale)
    assert stale.subscription_cancel_at == _naive(END)
    assert "  stale@example.com  cus_stale  no live subscription in Stripe, left as it is" in lines


def test_a_stripe_error_is_reported_and_the_rest_carry_on(db_session, stripe_subs):
    broken = _rider(db_session, "broken@example.com", "cus_broken", cancel_at=_naive(END))
    fine = _rider(db_session, "fine@example.com", "cus_fine")
    stripe_subs[DOWN] = ["cus_broken"]
    stripe_subs["cus_fine"] = [_sub("f", cancel_at=END)]

    code, lines = _run(db_session, "--commit")
    assert code == 1
    db_session.refresh(broken)
    db_session.refresh(fine)
    assert broken.subscription_cancel_at == _naive(END)  # untouched
    assert fine.subscription_cancel_at == _naive(END)
    assert "  broken@example.com  cus_broken  couldn't read Stripe (No such customer)" in lines
    assert lines[-1] == "Changed 1, unchanged 0, couldn't read 1."


def test_without_the_stripe_keys_nothing_is_read_or_changed(db_session, monkeypatch):
    monkeypatch.setattr(settings, "stripe_secret_key", "")
    portal = _rider(db_session, "portal@example.com", "cus_portal")
    code, lines = _run(db_session, "--commit")
    assert code == 1
    assert lines == ["The Stripe keys aren't loaded, so nothing was read or changed."]
    db_session.refresh(portal)
    assert portal.subscription_cancel_at is None


def test_what_it_prints_has_no_dashes_or_exclamation_marks(db_session, stripe_subs):
    _rider(db_session, "portal@example.com", "cus_portal")
    stripe_subs["cus_portal"] = [_sub("p", cancel_at=END)]
    _, lines = _run(db_session)
    text = "\n".join(lines) + backfill.__doc__
    assert not any(ch in text for ch in ("\u2013", "\u2014", "!")), text
