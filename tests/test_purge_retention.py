"""The account purge keeps the safety records: holds, red-flag events,
consent and health screening survive the account, stamped with
subject_deleted_at, and a second pass deletes them three years later.

purge_service reads the schema from Postgres's information_schema. Here the
selection logic is tested directly, and the full purge runs on SQLite with
the schema read through SQLAlchemy's inspector instead."""

from datetime import date, datetime, time, timedelta, timezone

import pytest
from sqlalchemy import inspect, text

from app.models.base import Base
from app.models.ride import Ride, RideSource
from app.models.safety import (
    SAFETY_RETAINED_TABLES,
    ClearanceLimit,
    ConsentEvent,
    HealthScreening,
    RideSessionStart,
    SafetyEvent,
    SafetyHold,
)
from app.models.user import User
from app.services import gdpr_service
from app.services import purge_service as ps

RETAINED = sorted(SAFETY_RETAINED_TABLES)


# === Which tables the purge deletes, and which it stamps ===


def test_safety_tables_are_never_purge_targets():
    owned = {"users", "rides", "founding_ledger", "ride_session_starts", *RETAINED}
    fks = [("ride_streams", "ride_id", "rides", "id")]
    targets = ps._targets(owned, fks)
    assert set(targets) == {"rides", "ride_session_starts", "ride_streams"}
    assert not SAFETY_RETAINED_TABLES & set(targets)


def test_retained_tables_need_the_stamp_column():
    owned = {"users", "rides", *RETAINED}
    columns = {(t, "subject_deleted_at") for t in RETAINED} | {("rides", "user_id")}
    assert ps._retained(owned, columns) == RETAINED
    # Before the migration has run there is nothing to stamp.
    assert ps._retained(owned, {("rides", "user_id")}) == []
    assert ps._retained({"users"}, columns) == []


def test_three_years_back_from_a_leap_day():
    assert ps._years_before(datetime(2028, 2, 29, 12), 3) == datetime(2025, 2, 28, 12)
    assert ps._years_before(datetime(2029, 10, 8), 3) == datetime(2026, 10, 8)


# === The full purge, on SQLite ===


def _sqlite_schema(db):
    """What purge_service._schema reads from information_schema, read
    through the inspector so it works on SQLite."""
    insp = inspect(db.get_bind())
    owned, fks, columns = set(), [], set()
    for table in insp.get_table_names():
        for col in insp.get_columns(table):
            columns.add((table, col["name"]))
            if col["name"] == "user_id":
                owned.add(table)
        for fk in insp.get_foreign_keys(table):
            for col, pcol in zip(fk["constrained_columns"], fk["referred_columns"]):
                fks.append((table, col, fk["referred_table"], pcol))
    return owned, fks, columns


def _rider(db, email: str, deleted_days_ago: int | None = None) -> User:
    user = User(email=email, hashed_password="x")
    if deleted_days_ago is not None:
        user.deleted_at = datetime.utcnow() - timedelta(days=deleted_days_ago)
    db.add(user)
    db.commit()
    db.add_all([
        Ride(user_id=user.id, source=RideSource.manual, ride_date=datetime.utcnow()),
        RideSessionStart(user_id=user.id, steps_hash="a" * 64, ftp=250, erg=True),
        SafetyHold(user_id=user.id, level="hold_all", reason="Chest pain", source="detector"),
        SafetyEvent(user_id=user.id, kind="chest_pain", source="chat", matched="chest pain"),
        ConsentEvent(user_id=user.id, kind="terms", doc_version="terms-2026-10-draft",
                     text_shown="I agree to Forma's terms.", source="register"),
        HealthScreening(user_id=user.id, version="screen-v1", answers={"q2": True},
                        any_yes=True, tier="hold_all"),
    ])
    db.commit()
    return user


SAFETY_MODELS = (SafetyHold, SafetyEvent, ConsentEvent, HealthScreening)


@pytest.fixture
def sqlite_schema(monkeypatch):
    monkeypatch.setattr(ps, "_schema", _sqlite_schema)


def test_purge_keeps_and_stamps_the_safety_records(db_session, sqlite_schema):
    gone = _rider(db_session, "gone@example.com")
    stays = _rider(db_session, "stays@example.com")
    gone_id, stays_id = gone.id, stays.id

    ps.purge_user(db_session, gone_id)
    db_session.commit()
    db_session.expire_all()

    assert db_session.get(User, gone_id) is None
    assert db_session.query(Ride).filter_by(user_id=gone_id).count() == 0
    # What the trainer was told is ordinary rider data: it goes.
    assert db_session.query(RideSessionStart).filter_by(user_id=gone_id).count() == 0
    for model in SAFETY_MODELS:
        kept = db_session.query(model).filter_by(user_id=gone_id).one()
        assert kept.subject_deleted_at is not None, model.__tablename__

    # The other rider is untouched.
    assert db_session.get(User, stays_id) is not None
    assert db_session.query(RideSessionStart).filter_by(user_id=stays_id).count() == 1
    for model in SAFETY_MODELS:
        assert db_session.query(model).filter_by(user_id=stays_id).one().subject_deleted_at is None


def test_a_row_already_stamped_keeps_its_first_date(db_session, sqlite_schema):
    rider = _rider(db_session, "twice@example.com")
    first = datetime(2026, 1, 1)
    hold = db_session.query(SafetyHold).filter_by(user_id=rider.id).one()
    hold.subject_deleted_at = first
    db_session.commit()
    ps.purge_user(db_session, rider.id)
    db_session.commit()
    db_session.expire_all()
    assert db_session.get(SafetyHold, hold.id).subject_deleted_at == first


def test_the_daily_purge_stamps_rather_than_deletes(db_session, sqlite_schema):
    expired = _rider(db_session, "expired@example.com", deleted_days_ago=31)
    recent = _rider(db_session, "recent@example.com", deleted_days_ago=5)
    expired_id, recent_id = expired.id, recent.id

    assert ps.purge_expired_accounts(db_session) == 1
    db_session.expire_all()
    assert db_session.get(User, expired_id) is None
    assert db_session.get(User, recent_id) is not None
    for model in SAFETY_MODELS:
        assert db_session.query(model).filter_by(user_id=expired_id).one().subject_deleted_at
        # Stamped today, so the retention sweep in the same run left them.


# === The second pass: three years after the account went ===


def _stamped(db, user_id: str, when: datetime | None) -> None:
    db.add_all([
        SafetyHold(user_id=user_id, level="easy_only", reason="Knee", source="coach_tool",
                   subject_deleted_at=when),
        SafetyEvent(user_id=user_id, kind="fever", source="chat", matched="fever",
                    subject_deleted_at=when),
        ConsentEvent(user_id=user_id, kind="health_data", doc_version="terms-2026-10-draft",
                     text_shown="Forma can use the health details I share.", source="register",
                     subject_deleted_at=when),
        HealthScreening(user_id=user_id, version="screen-v1", answers={}, tier="none",
                        subject_deleted_at=when),
    ])
    db.commit()


def _left(db) -> set[str]:
    return {
        row.user_id
        for model in SAFETY_MODELS
        for row in db.query(model).all()
    }


def test_the_sweep_deletes_only_records_past_three_years(db_session):
    now = datetime(2030, 6, 1, 12)
    _stamped(db_session, "old", now - timedelta(days=3 * 365 + 2))
    _stamped(db_session, "young", now - timedelta(days=3 * 365 - 2))
    _stamped(db_session, "live", None)

    assert ps.purge_expired_safety_records(db_session, now=now) == 4
    db_session.expire_all()
    assert _left(db_session) == {"young", "live"}


def test_a_dry_run_sweep_counts_but_keeps(db_session):
    now = datetime(2030, 6, 1, 12)
    _stamped(db_session, "old", now - timedelta(days=4 * 365))
    assert ps.purge_expired_safety_records(db_session, commit=False, now=now) == 4
    assert _left(db_session) == {"old"}


def test_the_daily_purge_runs_the_sweep(db_session):
    _stamped(db_session, "old", datetime.utcnow() - timedelta(days=4 * 365))
    _stamped(db_session, "young", datetime.utcnow() - timedelta(days=30))
    ps.purge_expired_accounts(db_session, commit=False)
    assert _left(db_session) == {"old", "young"}
    ps.purge_expired_accounts(db_session)
    db_session.expire_all()
    assert _left(db_session) == {"young"}


# === Review finding 20: the exchange survives, and a minor's records last ===


def _add_retain_until(db) -> None:
    """The purge stamps retain_until where the column exists. Add it to the
    test database if the models don't carry it yet (see the contract test
    below), so the purge logic is tested either way."""
    insp = inspect(db.connection())
    for table in sorted(SAFETY_RETAINED_TABLES & set(insp.get_table_names())):
        if not any(c["name"] == "retain_until" for c in insp.get_columns(table)):
            db.execute(text(f'ALTER TABLE "{table}" ADD COLUMN retain_until DATETIME'))
    db.commit()


def _retain_until(db, user_id: str) -> set:
    out = set()
    for table in sorted(SAFETY_RETAINED_TABLES):
        for (value,) in db.execute(
            text(f'SELECT retain_until FROM "{table}" WHERE user_id = :uid'), {"uid": user_id}
        ):
            out.add(value if isinstance(value, datetime) else datetime.fromisoformat(str(value)))
    return out


def _born(years_ago: int, days: int = 0) -> date:
    today = datetime.utcnow().date()
    return ps._years_after(today, -years_ago) - timedelta(days=days)


@pytest.fixture
def stamped_schema(db_session, monkeypatch):
    _add_retain_until(db_session)
    monkeypatch.setattr(ps, "_schema", _sqlite_schema)


def test_the_safety_models_carry_retain_until():
    """Contract with app/models/safety.py and its migration: every retained
    table has retain_until, or a minor's account can't be purged (the purge
    refuses rather than lose the later date)."""
    for table in SAFETY_RETAINED_TABLES:
        assert "retain_until" in Base.metadata.tables[table].columns, table


def test_the_purge_keeps_the_riders_words_and_the_coachs_reply(db_session, stamped_schema):
    rider = _rider(db_session, "exchange@example.com")
    db_session.add(SafetyEvent(
        user_id=rider.id, kind="chest_pain", source="chat", matched="chest pain",
        rider_message="I had chest pain on the climb today, is it fine to ride tomorrow?",
        coach_reply="Stop riding and call 999 if the pain comes back.",
    ))
    db_session.commit()
    rider_id = rider.id
    ps.purge_user(db_session, rider_id)
    db_session.commit()
    db_session.expire_all()

    kept = db_session.query(SafetyEvent).filter(
        SafetyEvent.user_id == rider_id, SafetyEvent.rider_message.isnot(None)
    ).one()
    assert kept.rider_message.startswith("I had chest pain on the climb")
    assert kept.coach_reply == "Stop riding and call 999 if the pain comes back."
    assert kept.subject_deleted_at is not None


def test_the_purge_keeps_the_doctors_limits(db_session, stamped_schema):
    rider = _rider(db_session, "limits@example.com")
    db_session.add(ClearanceLimit(user_id=rider.id, limits="No sprints for six weeks",
                                  cleared_by="Cardiologist"))
    db_session.commit()
    rider_id = rider.id
    ps.purge_user(db_session, rider_id)
    db_session.commit()
    db_session.expire_all()
    kept = db_session.query(ClearanceLimit).filter_by(user_id=rider_id).one()
    assert kept.limits == "No sprints for six weeks" and kept.subject_deleted_at is not None


def _minor_rider(db, email: str, born: date | None, flag: str | None = "hold") -> str:
    rider = _rider(db, email)
    rider.date_of_birth = born
    if flag == "hold":
        db.add(SafetyHold(user_id=rider.id, level="hold_all", reason="Said they are 15",
                          red_flag="minor", source="detector"))
    elif flag == "event":
        db.add(SafetyEvent(user_id=rider.id, kind="minor", source="coach_tool",
                           matched="minor"))
    db.commit()
    return rider.id


def test_an_adult_is_kept_three_years(db_session, stamped_schema):
    rider = _rider(db_session, "adult@example.com")
    rider.date_of_birth = _born(40)
    db_session.commit()
    rider_id = rider.id
    before = datetime.utcnow()
    ps.purge_user(db_session, rider_id)
    db_session.commit()
    (until,) = _retain_until(db_session, rider_id)
    assert ps._years_after(before, 3) <= until <= ps._years_after(datetime.utcnow(), 3)


def test_a_minor_is_kept_until_their_21st_birthday(db_session, stamped_schema):
    born = _born(15)
    rider_id = _minor_rider(db_session, "minor@example.com", born)
    ps.purge_user(db_session, rider_id)
    db_session.commit()
    birthday = ps._years_after(born, 21)
    assert _retain_until(db_session, rider_id) == {
        datetime.combine(birthday + timedelta(days=1), time.min)
    }

    # Three years on, the adult rule would have deleted them. Not for a minor.
    three_years = ps._years_after(datetime.utcnow(), 3) + timedelta(days=2)
    assert ps.purge_expired_safety_records(db_session, now=three_years) == 0
    assert _left(db_session) == {rider_id}
    # Still there on the 21st birthday itself; gone the day after.
    on_the_day = datetime.combine(birthday, time(23, 59))
    assert ps.purge_expired_safety_records(db_session, now=on_the_day) == 0
    after = datetime.combine(birthday + timedelta(days=1), time(0, 1))
    assert ps.purge_expired_safety_records(db_session, now=after) > 0
    db_session.expire_all()
    assert _left(db_session) == set()


def test_a_minor_flag_from_a_red_flag_event_counts(db_session, stamped_schema):
    born = _born(16)
    rider_id = _minor_rider(db_session, "event@example.com", born, flag="event")
    ps.purge_user(db_session, rider_id)
    db_session.commit()
    expected = datetime.combine(ps._years_after(born, 21) + timedelta(days=1), time.min)
    assert _retain_until(db_session, rider_id) == {expected}


def test_a_minor_close_to_21_still_gets_the_three_years(db_session, stamped_schema):
    # Said 17 two years ago: 21 within two years, so three years from the
    # purge is the later date.
    rider_id, event_id = _stated_minor(db_session, "nearly@example.com", 17, _born(19))
    event = db_session.get(SafetyEvent, event_id)
    event.created_at = ps._years_after(datetime.utcnow(), -2)
    db_session.commit()
    before = datetime.utcnow()
    ps.purge_user(db_session, rider_id)
    db_session.commit()
    (until,) = _retain_until(db_session, rider_id)
    assert ps._years_after(before, 3) <= until <= ps._years_after(datetime.utcnow(), 3)


def test_a_minor_with_no_date_of_birth_and_no_age_is_kept_as_a_13_year_olds(
    db_session, stamped_schema
):
    """Reverify round 3, problem 9: an under-18 hold with nothing behind it
    (no event, no words, no date of birth) used to keep only the three
    years. It is now kept as if the rider were 13 when flagged: eight years."""
    rider_id = _minor_rider(db_session, "nodob@example.com", None)
    opened = db_session.query(SafetyHold).filter_by(user_id=rider_id, red_flag="minor").one().opened_at
    ps.purge_user(db_session, rider_id)
    db_session.commit()
    expected = datetime.combine(ps._years_after(opened.date(), 8) + timedelta(days=1), time.min)
    assert _retain_until(db_session, rider_id) == {expected}


def test_a_date_of_birth_under_18_at_sign_up_flags_the_account(db_session, stamped_schema):
    born = _born(16)
    rider_id = _minor_rider(db_session, "young@example.com", born, flag=None)
    ps.purge_user(db_session, rider_id)
    db_session.commit()
    expected = datetime.combine(ps._years_after(born, 21) + timedelta(days=1), time.min)
    assert _retain_until(db_session, rider_id) == {expected}


def test_an_adult_who_joined_at_18_is_not_a_minor(db_session):
    rider = _rider(db_session, "eighteen@example.com")
    rider.date_of_birth = _born(18, days=3)
    db_session.commit()
    assert not ps.flagged_as_minor(
        db_session, rider.id, rider.date_of_birth, rider.created_at, set(SAFETY_RETAINED_TABLES)
    )


def test_a_minor_is_never_purged_without_somewhere_to_keep_the_date(db_session, monkeypatch):
    """Without retain_until the 21st birthday would be lost with the users
    row. The purge refuses, the account stays whole, and the daily run
    leaves it for the next day rather than failing everyone else."""
    def no_retain_until(db):
        owned, fks, columns = _sqlite_schema(db)
        return owned, fks, {c for c in columns if c[1] != "retain_until"}

    monkeypatch.setattr(ps, "_schema", no_retain_until)
    rider_id = _minor_rider(db_session, "nocolumn@example.com", _born(15))
    with pytest.raises(RuntimeError, match="retain_until"):
        ps.purge_user(db_session, rider_id)
    db_session.rollback()
    assert db_session.get(User, rider_id) is not None

    adult = _rider(db_session, "fine@example.com")
    adult_id = adult.id
    ps.purge_user(db_session, adult_id)  # an adult needs no later date
    db_session.commit()
    assert db_session.get(User, adult_id) is None


def test_a_leap_day_birthday_reaches_21_on_1_march():
    assert ps._years_after(date(2008, 2, 29), 21) == date(2029, 3, 1)
    assert ps._years_after(date(2008, 2, 29), 20) == date(2028, 2, 29)


def test_rows_stamped_before_retain_until_existed_still_go_after_three_years(db_session):
    _add_retain_until(db_session)
    now = datetime(2030, 6, 1, 12)
    _stamped(db_session, "legacy", now - timedelta(days=3 * 365 + 2))
    assert ps.purge_expired_safety_records(db_session, now=now) == 4
    assert _left(db_session) == set()


# === R20 remainder: the age a minor told us decides how long their records last ===
#
# A real minor can only have joined by giving a false adult date of birth,
# so the date of birth says "adult" and the records went three years after
# the purge, while the rider's claims run to their 21st birthday. The age
# they stated when the flag was raised now decides it
# (safety_service.minor_retention_until), and a minor with no stated age is
# kept as if 13 when flagged (reverify round 3, problem 9).


def _stated_minor(db, email: str, stated_age: int | None, born: date | None) -> tuple[str, str]:
    """A rider with a minor hold and the red-flag event that raised it,
    stating `stated_age`, and `born` as the date of birth given at sign-up."""
    rider = _rider(db, email)
    rider.date_of_birth = born
    # With no stated age, words that give none either: an older record's
    # age is read again from the rider's words (minor_retention_until).
    said = f"I'm {stated_age}, is" if stated_age is not None else "Is"
    hold = SafetyHold(user_id=rider.id, level="hold_all", reason="Said they are under 18",
                      red_flag="minor", source="detector")
    db.add(hold)
    db.flush()
    # The event points at the hold it opened, as the detector's do.
    event = SafetyEvent(
        user_id=rider.id, kind="minor", source="chat",
        matched=f"i'm {stated_age}" if stated_age is not None else "school races",
        rider_message=f"{said} this plan OK for school races?", stated_age=stated_age,
        hold_id=hold.id,
    )
    db.add(event)
    db.commit()
    return rider.id, event.id


def test_a_self_declared_15_year_old_is_kept_through_their_21st_birthday(
    db_session, stamped_schema
):
    """The reverify probe: an adult date of birth at sign-up, then "I'm 15"
    in chat. Before the fix the records went at purge plus three years."""
    from app.services import safety_service as ss

    rider_id, event_id = _stated_minor(db_session, "fifteen@example.com", 15, _born(30))
    event = db_session.get(SafetyEvent, event_id)
    said = event.created_at.date()
    expected = ps._kept_through(ss.minor_retention_until(event))
    assert expected is not None
    # Fifteen when they said it, so 21 no later than six years on: kept at
    # least that long, and not much longer.
    assert datetime.combine(ps._years_after(said, 6), time.min) <= expected
    assert expected <= datetime.combine(ps._years_after(said, 7), time.min)

    ps.purge_user(db_session, rider_id)
    db_session.commit()
    assert _retain_until(db_session, rider_id) == {expected}
    assert expected > ps._years_after(datetime.utcnow(), 3) + timedelta(days=2)

    # Three years on, the adult rule would have deleted them. Not now.
    three_years = ps._years_after(datetime.utcnow(), 3) + timedelta(days=2)
    assert ps.purge_expired_safety_records(db_session, now=three_years) == 0
    assert _left(db_session) == {rider_id}
    # Gone once the 21st birthday has passed.
    assert ps.purge_expired_safety_records(
        db_session, now=expected + timedelta(minutes=1)
    ) > 0
    db_session.expire_all()
    assert _left(db_session) == set()


def test_a_minor_with_no_stated_age_and_an_adult_date_of_birth_is_kept_as_a_13_year_olds(
    db_session, stamped_schema
):
    """Reverify round 3, problem 9: the coach's own judgement, with no age in
    the rider's words and a false adult date of birth, kept only the three
    years (to 2029). Now the youngest plausible age, 13, sets the date."""
    from app.services import safety_service as ss

    rider_id, event_id = _stated_minor(db_session, "noage@example.com", None, _born(30))
    event = db_session.get(SafetyEvent, event_id)
    said = event.created_at.date()
    expected = ps._kept_through(ss.minor_retention_until(event))
    assert expected == datetime.combine(ps._years_after(said, 8) + timedelta(days=1), time.min)
    ps.purge_user(db_session, rider_id)
    db_session.commit()
    assert _retain_until(db_session, rider_id) == {expected}


def test_a_hold_forma_put_on_by_hand_keeps_a_minors_records_to_the_age_rule(
    db_session, stamped_schema
):
    """--close-minor --force writes the under-18 hold with no event behind
    it. The purge reads it through safety_service.minor_retention_end: with
    no age, as a 13-year-old's; with the age Forma recorded (--age), to that
    age's 21st birthday."""
    rider = _rider(db_session, "forced@example.com")
    rider.date_of_birth = _born(30)
    hold = SafetyHold(user_id=rider.id, level="hold_all", reason="Closed as under 18 on review",
                      red_flag="minor", source="admin")
    db_session.add(hold)
    db_session.commit()
    opened = hold.opened_at
    told = _rider(db_session, "forced16@example.com")
    told.date_of_birth = _born(30)
    told_hold = SafetyHold(user_id=told.id, level="hold_all", reason="Closed as under 18",
                           red_flag="minor", source="admin")
    db_session.add(told_hold)
    db_session.flush()
    db_session.add(SafetyEvent(user_id=told.id, kind="minor", source="admin", matched="Age 16",
                               stated_age=16, hold_id=told_hold.id))
    db_session.commit()
    told_on = (
        db_session.query(SafetyEvent).filter_by(user_id=told.id, kind="minor").one().created_at.date()
    )
    rider_id, told_id = rider.id, told.id
    for uid in (rider_id, told_id):
        ps.purge_user(db_session, uid)
        db_session.commit()
    assert _retain_until(db_session, rider_id) == {
        datetime.combine(ps._years_after(opened.date(), 8) + timedelta(days=1), time.min)
    }
    assert _retain_until(db_session, told_id) == {
        datetime.combine(ps._years_after(told_on, 5) + timedelta(days=1), time.min)
    }


def test_an_older_minor_record_with_no_stated_age_is_kept_by_the_age_in_its_words(
    db_session, stamped_schema
):
    """A minor record written before stated_age was kept: the purge reads the
    age from the rider's own words, so "I'm 15" still runs to their 21st."""
    from app.services import safety_service as ss

    rider_id, event_id = _stated_minor(db_session, "older15@example.com", None, _born(30))
    event = db_session.get(SafetyEvent, event_id)
    event.rider_message = "I'm 15, is this plan OK for school races?"
    db_session.commit()
    expected = ps._kept_through(ss.minor_retention_until(event))
    said = event.created_at.date()
    assert expected == datetime.combine(ps._years_after(said, 6) + timedelta(days=1), time.min)
    ps.purge_user(db_session, rider_id)
    db_session.commit()
    assert _retain_until(db_session, rider_id) == {expected}


def test_the_later_of_the_stated_age_and_the_date_of_birth_wins(db_session, stamped_schema):
    from app.services import safety_service as ss

    # Said 15, date of birth 17: the stated age runs later.
    young_id, event_id = _stated_minor(db_session, "said15@example.com", 15, _born(17))
    stated = ps._kept_through(ss.minor_retention_until(db_session.get(SafetyEvent, event_id)))
    # Said 17, date of birth 14: the date of birth runs later.
    born = _born(14)
    old_id, _ = _stated_minor(db_session, "said17@example.com", 17, born)
    for uid in (young_id, old_id):
        ps.purge_user(db_session, uid)
        db_session.commit()
    assert _retain_until(db_session, young_id) == {stated}
    assert _retain_until(db_session, old_id) == {
        datetime.combine(ps._years_after(born, 21) + timedelta(days=1), time.min)
    }


def test_the_purge_uses_whatever_minor_retention_end_says(
    db_session, stamped_schema, monkeypatch
):
    """The purge's side of the contract, whatever the rule inside
    safety_service (minor_retention_end, over every under-18 event and every
    hold no event points to): its date counts when later than three years,
    None means three years, a plain date is the last day kept, and the purge
    passes its own time as the deletion time."""
    from app.services import safety_service as ss

    far = datetime(2040, 5, 6, 7, 8)
    answers = {
        "far@example.com": far,
        "date@example.com": date(2038, 1, 1),
        "aware@example.com": datetime(2041, 1, 1, 9, tzinfo=timezone.utc),
        "unknown@example.com": None,
    }
    asked = []

    def end(db, user_id, deleted_at=None):
        asked.append(deleted_at)
        return answers[db.get(User, user_id).email]

    monkeypatch.setattr(ss, "minor_retention_end", end, raising=False)

    def rider(email: str) -> str:
        r = _rider(db_session, email)
        r.date_of_birth = _born(30)
        db_session.add(SafetyHold(user_id=r.id, level="hold_all", reason="Under 18",
                                  red_flag="minor", source="detector"))
        db_session.commit()
        return r.id

    ids = {email: rider(email) for email in answers}
    before = datetime.utcnow()
    for uid in ids.values():
        # One account per transaction, as purge_expired_accounts runs them.
        ps.purge_user(db_session, uid)
        db_session.commit()
    assert len(asked) == 4 and all(before <= when <= datetime.utcnow() for when in asked)
    assert _retain_until(db_session, ids["far@example.com"]) == {far}
    assert _retain_until(db_session, ids["date@example.com"]) == {datetime(2038, 1, 2)}
    assert _retain_until(db_session, ids["aware@example.com"]) == {datetime(2041, 1, 1, 9)}
    (until,) = _retain_until(db_session, ids["unknown@example.com"])
    assert ps._years_after(before, 3) <= until <= ps._years_after(datetime.utcnow(), 3)


def test_an_adult_with_no_minor_flag_never_asks_for_a_stated_age(
    db_session, stamped_schema, monkeypatch
):
    from app.services import safety_service as ss

    def never(*_, **__):
        raise AssertionError("asked about an adult's age")

    monkeypatch.setattr(ss, "minor_retention_until", never, raising=False)
    monkeypatch.setattr(ss, "minor_retention_end", never, raising=False)
    rider = _rider(db_session, "adult2@example.com")
    rider.date_of_birth = _born(40)
    db_session.commit()
    ps.purge_user(db_session, rider.id)
    db_session.commit()


# === A data request covers every table that holds a rider's data ===


def _rider_tables() -> set[str]:
    """Every table with a user_id, and every table hanging off one of
    those, to any depth, across every model module (not only the ones some
    other test happened to import)."""
    import importlib
    import pkgutil

    import app.models

    for module in pkgutil.iter_modules(app.models.__path__):
        importlib.import_module(f"app.models.{module.name}")
    tables = Base.metadata.tables
    owned = {name for name, t in tables.items() if "user_id" in t.columns} | {"users"}
    changed = True
    while changed:
        changed = False
        for name, table in tables.items():
            if name in owned:
                continue
            if any(fk.column.table.name in owned for fk in table.foreign_keys):
                owned.add(name)
                changed = True
    return owned


def test_every_riders_table_is_exported_or_deliberately_left_out():
    missing = sorted(
        t for t in _rider_tables()
        if t not in gdpr_service.EXPORTED_TABLES and t not in gdpr_service.NOT_EXPORTED
    )
    assert not missing, f"not in the data export: {missing}"
    assert SAFETY_RETAINED_TABLES | {"ride_session_starts"} <= set(gdpr_service.EXPORTED_TABLES)
    # Known by email, not user_id: the waitlist the rider joined first.
    assert {"waitlist", "waitlist_replies"} <= set(gdpr_service.EXPORTED_TABLES)


def test_the_export_carries_every_safety_record(db_session):
    rider = _rider(db_session, "sar@example.com")
    db_session.add_all([
        SafetyEvent(user_id=rider.id, kind="fainting", source="chat", matched="fainted",
                    rider_message="I nearly fainted on the turbo.",
                    coach_reply="Stop riding today and get checked."),
        ClearanceLimit(user_id=rider.id, limits="Nothing above tempo", cleared_by="GP"),
    ])
    db_session.commit()
    from app.models.waitlist import WaitlistEntry, WaitlistReply

    entry = WaitlistEntry(email="SAR@Example.com", name="Sam", goal="Fred Whitton")
    db_session.add(entry)
    db_session.flush()
    db_session.add(WaitlistReply(waitlist_entry_id=entry.id, raw_text="Winter kills my form.",
                                 received_at=datetime(2026, 9, 1)))
    db_session.add(WaitlistEntry(email="someone.else@example.com"))
    db_session.commit()
    archive = gdpr_service.export_user_data(db_session, rider)
    assert set(gdpr_service.EXPORTED_TABLES.values()) <= set(archive)
    assert [w["goal"] for w in archive["waitlist"]] == ["Fred Whitton"]
    assert [r["raw_text"] for r in archive["waitlist_replies"]] == ["Winter kills my form."]
    for key in ("health_screenings", "consent_events", "safety_holds", "safety_events",
                "clearance_limits", "ride_session_starts"):
        assert archive[key], key
    (exchange,) = [e for e in archive["safety_events"] if e.get("rider_message")]
    assert exchange["rider_message"] == "I nearly fainted on the turbo."
    assert exchange["coach_reply"] == "Stop riding today and get checked."
    assert archive["clearance_limits"][0]["limits"] == "Nothing above tempo"
