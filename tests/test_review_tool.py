"""Under-18 handling, finished (re-verification S7), and the founder's review
tool (scripts/review_safety_event.py).

- Every chat turn on an account held as possibly under 18 gets fixed words,
  with no model call, ending in the closure and refund line.
- When the hold opens, any Stripe subscription is set to end at the close of
  the paid period, so no renewal is taken before review.
- The review tool lists unreviewed red-flag events, marks one reviewed,
  lifts a hold by hand (an adult read as a minor) and closes an under-18
  account: Stripe cancelled, the GDPR delete, events reviewed, refund steps.

No real model, Stripe or email calls: every one is a fake.
"""

from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest

from app.config import settings
from app.models.chat import ChatMessage, ChatRole
from app.models.safety import SafetyEvent, SafetyHold
from app.models.user import User
from app.services import billing_service, coach_service, safety_screen
from app.services import safety_service as ss
from scripts import review_safety_event as tool
from tests.test_coach_safety_flow import (  # noqa: F401  (model is a fixture)
    _Usage,
    _minor,
    _run,
    _session,
    _text,
    _user,
    model,
)

# The fixed ending the founder signed off, word for word.
CLOSING = (
    "This account is on hold and will be closed, and anything you've paid will be "
    "refunded. If you're 18 or over and this was a mistake, email "
    "gareth@ridewithforma.com and I'll sort it out."
)
CLOSING_FACT = (
    "This account is on hold and will be closed, and anything you've paid will be refunded."
)


# ── Fakes ───────────────────────────────────────────────────────────────────


class _Obj(dict):
    """A Stripe object: attribute access and to_dict, like the library's."""

    def __getattr__(self, key):
        try:
            return self[key]
        except KeyError as e:
            raise AttributeError(key) from e

    def to_dict(self):
        return dict(self)


class _Page:
    def __init__(self, items):
        self.items = items

    def auto_paging_iter(self):
        return iter(list(self.items))


class FakeStripe:
    """Subscriptions and charges for one customer, and a log of every write."""

    def __init__(self):
        self.subs: list[_Obj] = []
        self.charges: list[_Obj] = []
        self.writes: list[tuple] = []
        self.fail: set[str] = set()

    def add_sub(self, sid, status="active", **kw):
        sub = _Obj(id=sid, status=status, cancel_at_period_end=False, metadata={},
                   current_period_end=1_900_000_000, **kw)
        self.subs.append(sub)
        return sub

    def add_charge(self, cid, amount, refunded=0, status="succeeded", pi=None):
        self.charges.append(_Obj(
            id=cid, amount=amount, amount_refunded=refunded, status=status, paid=True,
            currency="gbp", created=1_791_000_000, payment_intent=pi,
        ))

    def sub(self, sid):
        return next(s for s in self.subs if s["id"] == sid)

    # stripe.Subscription
    def list_subs(self, customer=None, status=None, limit=None):
        if "list" in self.fail:
            raise RuntimeError("Stripe is down")
        return _Page(self.subs)

    def modify(self, sid, **kw):
        if "modify" in self.fail:
            raise RuntimeError("Stripe is down")
        self.writes.append(("modify", sid, kw))
        sub = self.sub(sid)
        for key, value in kw.items():
            if key == "metadata":
                meta = dict(sub["metadata"])
                for k, v in value.items():
                    if v == "":
                        meta.pop(k, None)
                    else:
                        meta[k] = v
                sub["metadata"] = meta
            else:
                sub[key] = value
        return sub

    def cancel(self, sid, **kw):
        if "cancel" in self.fail:
            raise RuntimeError("Stripe is down")
        self.writes.append(("cancel", sid, kw))
        self.sub(sid)["status"] = "canceled"
        return self.sub(sid)

    # stripe.Charge
    def list_charges(self, customer=None, limit=None):
        return _Page(self.charges)


@pytest.fixture
def stripe_fake(monkeypatch):
    fake = FakeStripe()
    monkeypatch.setattr(settings, "stripe_secret_key", "sk_test_forma")
    monkeypatch.setattr(settings, "stripe_price_id", "price_forma")
    s = billing_service.stripe
    monkeypatch.setattr(s.Subscription, "list", fake.list_subs)
    monkeypatch.setattr(s.Subscription, "modify", fake.modify)
    monkeypatch.setattr(s.Subscription, "cancel", fake.cancel)
    monkeypatch.setattr(s.Charge, "list", fake.list_charges)

    def _never(*a, **k):
        raise AssertionError("refunds are made by hand in the dashboard, never from code")

    monkeypatch.setattr(s.Refund, "create", _never)
    # The renewal stop runs on a thread in production; inline here.
    monkeypatch.setattr(coach_service, "_run_in_background", lambda fn, *a: fn(*a))
    return fake


def _no_dashes_or_bangs(text: str) -> None:
    assert not any(ch in text for ch in ("—", "–", "!")), text


def _event(db, user, kind="minor", **kw) -> SafetyEvent:
    kw.setdefault("matched", "I'm 16 and a half stone")
    kw.setdefault("source", "chat")
    event = SafetyEvent(user_id=user.id, kind=kind, **kw)
    db.add(event)
    db.commit()
    return event


def _tool(db, *argv):
    return tool.run(db, tool.parse_args(list(argv)))


# ── The fixed reply ─────────────────────────────────────────────────────────


def test_a_held_account_gets_the_closure_line_on_every_chat_path_with_no_model_call(
    db_session, model
):
    user = _user(db_session)
    _minor(db_session, user)
    streamed = _text(_run(coach_service.stream_response(
        db_session, user, _session(db_session, user), "Can you build me a race plan?"
    )))
    voiced = _text(_run(coach_service.stream_voice_response(
        db_session, user, _session(db_session, user), "What intervals should I do?"
    )))
    synced = coach_service.get_non_streaming_response(
        db_session, user, _session(db_session, user), "How hard should I go?"
    )
    for reply in (streamed, voiced, synced):
        assert "Forma is for adults, 18 and over" in reply
        assert reply.endswith(CLOSING)
        assert reply.count("This account is on hold") == 1
        _no_dashes_or_bangs(reply)
    assert coach_service.MINOR_CLOSING == CLOSING
    assert model["calls"] == [] and model["memory"] == 0
    # What was saved is what the rider saw.
    saved = db_session.query(ChatMessage).filter(ChatMessage.role == ChatRole.assistant).all()
    assert all(m.content.endswith(CLOSING) for m in saved)


def test_the_message_that_opens_the_hold_ends_the_same_way_and_stops_renewal_once(
    db_session, model, stripe_fake
):
    stripe_fake.add_sub("sub_live")
    user = _user(db_session, stripe_customer_id="cus_kid")
    reply = _text(_run(coach_service.stream_response(
        db_session, user, _session(db_session, user), "I'm 15 and I want to race nationals"
    )))
    assert reply.endswith(CLOSING)
    assert model["calls"] == []
    sub = stripe_fake.sub("sub_live")
    assert sub["cancel_at_period_end"] is True
    assert sub["metadata"] == {billing_service.MINOR_REVIEW_FLAG: billing_service.MINOR_REVIEW_VALUE}
    assert sub["status"] == "active"  # ends at the period end; the review cancels outright

    # A later turn on the held account doesn't go back to Stripe.
    writes = len(stripe_fake.writes)
    _run(coach_service.stream_response(
        db_session, user, _session(db_session, user), "Please can you coach me?"
    ))
    assert len(stripe_fake.writes) == writes == 1


def test_a_child_in_crisis_hears_the_closure_but_the_reply_still_ends_with_people(
    db_session, model
):
    user = _user(db_session)
    reply = _text(_run(coach_service.stream_response(
        db_session, user, _session(db_session, user), "I'm 15 and I want to end it all"
    )))
    assert CLOSING_FACT in reply
    assert "gareth" not in reply.lower()
    assert not reply.endswith(CLOSING_FACT)
    assert "someone you trust" in reply.split("\n\n")[-1]
    assert model["calls"] == []


def test_when_the_coach_puts_the_hold_on_the_turn_ends_with_the_closure_and_renewal_stops(
    db_session, model, stripe_fake, monkeypatch
):
    stripe_fake.add_sub("sub_live")

    class _ToolFinal:
        content = [SimpleNamespace(
            type="tool_use", id="t1", name="apply_safety_hold",
            input={"level": "easy_only", "reason": "Rider said they are in year 13", "red_flag": "minor"},
        )]
        stop_reason = "tool_use"
        usage = _Usage()

    class _EndFinal:
        content = []
        stop_reason = "end_turn"
        usage = _Usage()

    class _S:
        def __init__(self, texts, final):
            self.texts, self.final = texts, final

        def __iter__(self):
            for t in self.texts:
                yield SimpleNamespace(type="content_block_delta", delta=SimpleNamespace(text=t))

        def get_final_message(self):
            return self.final

    script = [
        _S(["Thanks for telling me. "], _ToolFinal()),
        _S(["Forma is for adults, 18 and over, so I can't coach you."], _EndFinal()),
    ]

    @contextmanager
    def fake_stream(**kw):
        model["calls"].append(kw)
        yield script.pop(0)

    monkeypatch.setattr(coach_service.forma_core, "stream", fake_stream)
    user = _user(db_session, stripe_customer_id="cus_kid")
    reply = _text(_run(coach_service.stream_response(
        db_session, user, _session(db_session, user), "Can you train me for nationals?"
    )))
    assert reply.endswith("\n\n" + CLOSING)
    hold = ss.minor_hold(db_session, user)
    assert hold is not None and hold.level == "hold_all"
    assert stripe_fake.sub("sub_live")["cancel_at_period_end"] is True

    # The next turn is fixed words, with no model call.
    calls = len(model["calls"])
    nxt = _text(_run(coach_service.stream_response(
        db_session, user, _session(db_session, user), "Can you build me a plan now?"
    )))
    assert nxt.endswith(CLOSING) and len(model["calls"]) == calls


def test_stripe_being_down_never_costs_the_reply(db_session, model, stripe_fake):
    stripe_fake.add_sub("sub_live")
    stripe_fake.fail = {"list", "modify"}
    user = _user(db_session, stripe_customer_id="cus_kid")
    reply = _text(_run(coach_service.stream_response(
        db_session, user, _session(db_session, user), "I'm 16 years old and want a plan"
    )))
    assert reply.endswith(CLOSING)
    assert stripe_fake.sub("sub_live")["cancel_at_period_end"] is False


def test_the_age_the_rider_gave_goes_on_the_record(db_session, model, monkeypatch):
    """R20: how long an under-18 record is kept turns on the age they said."""
    user = _user(db_session)
    _run(coach_service.stream_response(
        db_session, user, _session(db_session, user), "I'm 15 and I want to race nationals"
    ))
    event = db_session.query(SafetyEvent).filter(SafetyEvent.kind == "minor").one()
    assert event.stated_age == 15
    assert ss.minor_retention_until(event) is not None
    # The coach's own reader is the detector's, not a stand-in.
    assert coach_service._stated_age("I'm 15 and I want to race nationals") == 15
    assert coach_service._stated_age("I'm 16 and a half stone") is None


# ── Billing ─────────────────────────────────────────────────────────────────


def test_renewal_stops_only_on_renewing_subscriptions_and_resumes_only_what_forma_stopped(
    stripe_fake,
):
    stripe_fake.add_sub("sub_active")
    stripe_fake.add_sub("sub_trial", status="trialing")
    stripe_fake.add_sub("sub_gone", status="canceled")
    own = stripe_fake.add_sub("sub_own_cancel")
    own["cancel_at_period_end"] = True  # the rider cancelled this one themselves
    rider = SimpleNamespace(id="u1", stripe_customer_id="cus_1")

    assert billing_service.stop_renewal_for_review(rider) == 2
    assert {w[1] for w in stripe_fake.writes} == {"sub_active", "sub_trial"}

    assert billing_service.resume_renewal_after_review(rider) == 2
    for sid in ("sub_active", "sub_trial"):
        assert stripe_fake.sub(sid)["cancel_at_period_end"] is False
        assert stripe_fake.sub(sid)["metadata"] == {}
    assert stripe_fake.sub("sub_own_cancel")["cancel_at_period_end"] is True


def test_stopping_renewal_never_raises(stripe_fake):
    stripe_fake.add_sub("sub_active")
    stripe_fake.fail = {"modify"}
    rider = SimpleNamespace(id="u1", stripe_customer_id="cus_1")
    assert billing_service.stop_renewal_for_review(rider) == 0
    assert billing_service.stop_renewal_for_review(SimpleNamespace(id="u2", stripe_customer_id=None)) == 0


def test_an_account_held_as_under_18_cannot_pay_or_turn_renewal_back_on(
    db_session, stripe_fake, monkeypatch
):
    opened = []
    monkeypatch.setattr(
        billing_service.stripe.checkout.Session, "create",
        lambda **kw: opened.append(("checkout", kw)) or SimpleNamespace(url="https://x"),
    )
    monkeypatch.setattr(
        billing_service.stripe.billing_portal.Session, "create",
        lambda **kw: opened.append(("portal", kw)) or SimpleNamespace(url="https://y"),
    )
    user = _user(db_session, stripe_customer_id="cus_kid")
    _minor(db_session, user)
    with pytest.raises(billing_service.AccountOnHold):
        billing_service.create_checkout_session(db_session, user)
    with pytest.raises(billing_service.AccountOnHold):
        billing_service.create_portal_session(db_session, user)
    assert opened == []

    # An adult with no such hold still gets both.
    adult = _user(db_session, "adult@example.com", stripe_customer_id="cus_adult")
    assert billing_service.create_checkout_session(db_session, adult) == "https://x"
    assert billing_service.create_portal_session(db_session, adult) == "https://y"


# ── The review tool: the list ───────────────────────────────────────────────


def test_the_list_shows_each_unreviewed_event_with_the_exchange(db_session):
    user = _user(db_session, "kid@example.com")
    hold = _minor(db_session, user)
    event = _event(
        db_session, user, hold_id=hold.id,
        rider_message="I'm 16 and a half stone and I want to get down to 13 " * 6,
        coach_reply="Forma is for adults, 18 and over.\n\n" + CLOSING,
    )
    done = _event(db_session, user, kind="chest_pain", matched="chest pain",
                  reviewed_at=datetime.utcnow(), review_note="fine")
    _event(db_session, user, kind="reply_failed", matched="none")

    code, lines = _tool(db_session)
    text = "\n".join(lines)
    assert code == 0
    assert "Unreviewed safety events: 1" in text
    assert "1 failed ordinary replies not shown" in text
    assert event.id in text and done.id not in text
    assert "kid@example.com" in text
    assert "kind: minor (chat)" in text and "I'm 16 and a half stone" in text
    rider_line = next(line for line in lines if line.strip().startswith("rider:"))
    assert rider_line.endswith("...") and len(rider_line) < 200
    assert "coach: Forma is for adults, 18 and over. / This account is on hold" in text
    assert f"hold: {hold.id} (open, admin_only)" in text
    assert "Open holds only Forma can lift: 1" in text
    assert f"  {hold.id}  kid@example.com  minor" in text
    _no_dashes_or_bangs(text)

    code, lines = _tool(db_session, "--include-failures")
    assert "Unreviewed safety events: 2" in "\n".join(lines)


def test_a_note_is_needed_to_review_or_lift(db_session):
    for argv in (["--reviewed", "e1"], ["--lift-hold", "h1"], ["--reviewed", "e1", "--note", "  "]):
        with pytest.raises(SystemExit):
            tool.parse_args(argv)


# ── The review tool: one event ──────────────────────────────────────────────


def test_marking_an_event_reviewed_is_a_dry_run_until_commit(db_session):
    user = _user(db_session)
    event = _event(db_session, user)

    code, lines = _tool(db_session, "--reviewed", event.id, "--note", "Read it; the reply was right")
    assert code == 0 and "Dry run" in lines[-1]
    db_session.expire_all()
    assert db_session.get(SafetyEvent, event.id).reviewed_at is None

    code, lines = _tool(
        db_session, "--reviewed", event.id, "--note", "Read it; the reply was right", "--commit"
    )
    assert code == 0 and lines[-1] == "Marked reviewed."
    db_session.expire_all()
    event = db_session.get(SafetyEvent, event.id)
    assert event.reviewed_at is not None
    assert "Read it; the reply was right" in (event.review_note or "")

    # A second look keeps the first review's date and adds a dated line.
    first = event.reviewed_at
    code, lines = _tool(db_session, "--reviewed", event.id, "--note", "Rider emailed, all well", "--commit")
    assert code == 0 and lines[-1] == "Note added to the review."
    db_session.expire_all()
    event = db_session.get(SafetyEvent, event.id)
    assert event.reviewed_at == first
    assert "Read it; the reply was right" in event.review_note
    assert "Rider emailed, all well" in event.review_note

    code, lines = _tool(db_session, "--reviewed", "nope", "--note", "x", "--commit")
    assert code == 1 and lines == ["No safety event nope."]


# ── The review tool: an adult read as a minor ───────────────────────────────


def test_lifting_a_wrong_under_18_hold_gives_the_adult_their_coach_and_renewal_back(
    db_session, model, stripe_fake
):
    stripe_fake.add_sub("sub_live")
    user = _user(db_session, "adult@example.com", stripe_customer_id="cus_adult")
    _run(coach_service.stream_response(
        db_session, user, _session(db_session, user), "I'm 17, no wait 47, and I race masters"
    ))
    hold = ss.minor_hold(db_session, user)
    if hold is None:  # the detector no longer misreads it: hold it by hand to test the lift
        hold = _minor(db_session, user)
        _event(db_session, user, hold_id=hold.id)
        billing_service.stop_renewal_for_review(user)
    assert stripe_fake.sub("sub_live")["cancel_at_period_end"] is True

    code, lines = _tool(db_session, "--lift-hold", hold.id, "--note", "Emailed me: 47, not 17")
    assert code == 0 and "Dry run: would lift it by hand" in lines[-1]
    assert ss.minor_hold(db_session, user) is not None

    code, lines = _tool(
        db_session, "--lift-hold", hold.id, "--note", "Emailed me: 47, not 17", "--commit"
    )
    text = "\n".join(lines)
    assert code == 0 and "Lifted." in text, text
    assert "Renewal back on for 1 subscription(s)." in text
    db_session.expire_all()
    assert ss.minor_hold(db_session, user) is None
    assert db_session.get(SafetyHold, hold.id).lifted_at is not None
    assert all(
        e.reviewed_at is not None
        for e in db_session.query(SafetyEvent).filter(SafetyEvent.hold_id == hold.id)
    )
    assert stripe_fake.sub("sub_live")["cancel_at_period_end"] is False

    # The coach is back: a model reply, and no closure line.
    calls = len(model["calls"])
    reply = _text(_run(coach_service.stream_response(
        db_session, user, _session(db_session, user), "How should I pace Saturday's crit?"
    )))
    assert len(model["calls"]) == calls + 1
    assert CLOSING not in reply

    code, lines = _tool(db_session, "--lift-hold", hold.id, "--note", "again", "--commit")
    assert code == 0 and "Already lifted" in lines[-1]


# ── The review tool: closing an under-18 account ────────────────────────────


def _child_with_payments(db, fake):
    fake.add_sub("sub_live")
    fake.add_sub("sub_old", status="canceled")
    fake.add_charge("ch_1", 1999, pi="pi_1")
    fake.add_charge("ch_2", 1999, refunded=1999, pi="pi_2")  # already refunded
    fake.add_charge("ch_3", 1999, status="failed", pi="pi_3")
    user = _user(db, "kid@example.com", stripe_customer_id="cus_kid")
    hold = _minor(db, user)
    _event(db, user, hold_id=hold.id)
    _event(db, user, kind="crisis", matched="want to end it all")
    return user, hold


def test_closing_an_under_18_account_dry_run_changes_nothing(db_session, stripe_fake):
    user, _ = _child_with_payments(db_session, stripe_fake)
    code, lines = _tool(db_session, "--close-minor", "Kid@Example.com")
    text = "\n".join(lines)
    assert code == 0
    assert "Live Stripe subscriptions found: 1" in text and "sub_live  active" in text
    assert "mark 2 unreviewed event(s) reviewed" in text
    assert "https://dashboard.stripe.com/test/payments/pi_1" in text
    assert stripe_fake.writes == []
    db_session.expire_all()
    user = db_session.get(User, user.id)
    assert user.is_active and user.deleted_at is None
    assert db_session.query(SafetyEvent).filter(SafetyEvent.reviewed_at.is_(None)).count() == 2


def test_closing_an_under_18_account_cancels_deletes_reviews_and_prints_the_refund_steps(
    db_session, stripe_fake
):
    user, hold = _child_with_payments(db_session, stripe_fake)
    code, lines = _tool(db_session, "--close-minor", "kid@example.com", "--commit")
    text = "\n".join(lines)
    assert code == 0, text

    assert stripe_fake.writes == [("cancel", "sub_live", {})]
    db_session.expire_all()
    user = db_session.get(User, user.id)
    assert user.is_active is False and user.deleted_at is not None
    events = db_session.query(SafetyEvent).filter(SafetyEvent.user_id == user.id).all()
    assert all(e.reviewed_at is not None for e in events)
    assert all(tool.CLOSE_NOTE in (e.review_note or "") for e in events)
    # The hold stays: the purge keeps the records to the under-18 rule.
    assert db_session.get(SafetyHold, hold.id).lifted_at is None

    assert "Cancelled 1 subscription(s) in Stripe." in text
    assert "1. Open https://dashboard.stripe.com/test/customers/cus_kid." in text
    assert "GBP 19.99  https://dashboard.stripe.com/test/payments/pi_1" in text
    assert "pi_2" not in text and "pi_3" not in text
    assert 'set the reason to "Requested by customer"' in text
    _no_dashes_or_bangs(text)


def test_closing_stops_with_nothing_changed_when_stripe_cannot_cancel(
    db_session, stripe_fake
):
    user, _ = _child_with_payments(db_session, stripe_fake)
    stripe_fake.fail = {"cancel"}
    code, lines = _tool(db_session, "--close-minor", "kid@example.com", "--commit")
    assert code == 1 and "nothing was changed" in lines[-1]
    db_session.expire_all()
    assert db_session.get(User, user.id).is_active is True
    assert db_session.query(SafetyEvent).filter(SafetyEvent.reviewed_at.is_(None)).count() == 2


def test_closing_needs_an_under_18_hold_or_force(db_session, stripe_fake):
    user = _user(db_session, "adult@example.com")
    code, lines = _tool(db_session, "--close-minor", "adult@example.com", "--commit")
    assert code == 1 and "has no open under-18 hold" in lines[0]
    db_session.expire_all()
    assert db_session.get(User, user.id).is_active is True

    code, lines = _tool(db_session, "--close-minor", "adult@example.com", "--commit", "--force")
    text = "\n".join(lines)
    assert code == 0, text
    db_session.expire_all()
    assert db_session.get(User, user.id).is_active is False
    hold = db_session.query(SafetyHold).filter(SafetyHold.user_id == user.id).one()
    assert (hold.red_flag, hold.source, hold.lifted_at) == ("minor", "admin", None)
    assert "no Stripe customer on this account" in text


# ── Reverify round 3: the review tool's gaps ────────────────────────────────


def test_stripe_commands_stop_when_the_keys_are_missing_but_the_rider_pays(
    db_session, monkeypatch
):
    """Under `railway run --service Postgres` alone the Stripe keys aren't
    loaded: --close-minor reported no subscriptions and "No payments are
    waiting for a refund", and --lift-hold turned no renewal back on. Now
    both stop with nothing changed, dry run or not."""
    monkeypatch.setattr(settings, "stripe_secret_key", "")
    monkeypatch.setattr(settings, "stripe_price_id", "")
    user = _user(db_session, "kid@example.com", stripe_customer_id="cus_kid")
    hold = _minor(db_session, user)
    for extra in ((), ("--commit",)):
        code, lines = _tool(db_session, "--close-minor", "kid@example.com", *extra)
        text = "\n".join(lines)
        assert code == 1 and "Stripe isn't configured here" in text and "cus_kid" in text
        assert "No payments are waiting" not in text
        code, lines = _tool(db_session, "--lift-hold", hold.id, "--note", "Adult", *extra)
        assert code == 1 and "Stripe isn't configured here" in "\n".join(lines)
        _no_dashes_or_bangs("\n".join(lines))
    db_session.expire_all()
    assert db_session.get(User, user.id).deleted_at is None
    assert ss.minor_hold(db_session, user).id == hold.id

    # No Stripe customer: nothing to read, so the tool goes on.
    free = _user(db_session, "free@example.com")
    _minor(db_session, free)
    code, lines = _tool(db_session, "--close-minor", "free@example.com")
    assert code == 0 and "no Stripe customer on this account" in "\n".join(lines)


def test_force_with_an_age_keeps_the_records_to_that_age(db_session, stripe_fake):
    """Reverify round 3, problem 9: --close-minor --force had no way to record
    an age, so the records went three years after the purge."""
    user = _user(db_session, "found@example.com")
    code, lines = _tool(
        db_session, "--close-minor", "found@example.com", "--force", "--age", "15"
    )
    assert code == 0 and "record age 15" in "\n".join(lines)
    assert db_session.query(SafetyEvent).filter(SafetyEvent.user_id == user.id).count() == 0

    code, lines = _tool(
        db_session, "--close-minor", "found@example.com", "--force", "--age", "15", "--commit"
    )
    text = "\n".join(lines)
    assert code == 0 and "Age 15 recorded on the under-18 record." in text, text
    db_session.expire_all()
    hold = db_session.query(SafetyHold).filter(SafetyHold.user_id == user.id).one()
    event = db_session.query(SafetyEvent).filter(SafetyEvent.user_id == user.id).one()
    assert (event.kind, event.source, event.stated_age, event.hold_id) == (
        "minor", "admin", 15, hold.id
    )
    assert event.reviewed_at is not None
    said = event.created_at
    assert ss.minor_retention_end(db_session, user.id, deleted_at=said).date() == (
        said.replace(year=said.year + 6).date() + timedelta(days=1)
    )
    _no_dashes_or_bangs(text)


def test_age_must_be_under_18_and_go_with_close_minor(capsys):
    for argv in (["--close-minor", "a@example.com", "--age", "18"],
                 ["--close-minor", "a@example.com", "--age", "0"],
                 ["--lift-hold", "h", "--note", "x", "--age", "15"]):
        with pytest.raises(SystemExit):
            tool.parse_args(argv)
    assert tool.parse_args(["--close-minor", "a@example.com", "--age", "17"]).age == 17


def test_lifting_a_wrong_under_18_hold_clears_a_childs_date_and_stays_lifted(
    db_session, stripe_fake, monkeypatch
):
    """An adult whose date of birth on file says 16 was held again at the
    next re-acceptance, and the detector's quiet window never started while
    that date stood (reverify round 3, problems 4 and 8)."""
    from datetime import date

    from app.services import plan_service

    monkeypatch.setattr(plan_service, "sync_hold_marks", lambda db, user_id, commit=True: None)
    today = datetime.utcnow().date()
    user = _user(db_session, "typo@example.com",
                 date_of_birth=date(today.year - 16, 1, 1), country="GB")
    hold = _minor(db_session, user)
    code, lines = _tool(db_session, "--lift-hold", hold.id, "--note", "Typo, born 1980")
    assert code == 0 and "clear the under-18 date of birth on file" in lines[-1]
    db_session.expire_all()
    assert db_session.get(User, user.id).date_of_birth is not None

    code, lines = _tool(
        db_session, "--lift-hold", hold.id, "--note", "Typo, born 1980", "--commit"
    )
    text = "\n".join(lines)
    assert code == 0 and "Cleared the date of birth on file" in text, text
    _no_dashes_or_bangs(text)
    db_session.expire_all()
    user = db_session.get(User, user.id)
    assert user.date_of_birth is None
    # The app asks for the date again, and the quiet window has started.
    from app.api.v1.auth import needs_profile_consent

    assert needs_profile_consent(user)
    assert ss.minor_quiet_until(db_session, user) is not None
    result = safety_screen.screen_message(db_session, user, "I'm 16 and I want to race")
    assert "minor" not in result.kinds and ss.minor_hold(db_session, user) is None

    # An adult's date of birth is left alone.
    adult = _user(db_session, "adult47@example.com", date_of_birth=date(1979, 5, 1))
    other = _minor(db_session, adult)
    code, lines = _tool(db_session, "--lift-hold", other.id, "--note", "47", "--commit")
    assert code == 0 and "Cleared the date" not in "\n".join(lines)
    db_session.expire_all()
    assert db_session.get(User, adult.id).date_of_birth == date(1979, 5, 1)
