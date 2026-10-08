"""The SAFETY LAW reaches every model call that can reach a rider, and the
prompt lines that contradicted it are gone.

forma_core is the single funnel to the model, so these tests drive it with a
fake provider client and look at exactly what would have been sent.
"""

import re
from contextlib import contextmanager
from pathlib import Path

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.config import settings
from app.core import coach_skills, forma_core
from app.core.coach_skills import SAFETY_LAW, SAFETY_LAW_VERSION
from app.core.constants import TSB_OVERTRAINING_THRESHOLD
from app.models.base import Base
from app.models.forma_call import FormaCall
from app.services import coach_service, outreach_service, safety_screen

# The tasks that never speak to a rider: memory extraction, chat titles and
# the safety classifier, which only returns JSON flags for code to merge.
EXEMPT = {"memory_extraction", "chat_title", "safety_classify"}
LAW_TASKS = sorted(set(forma_core.TASKS) - EXEMPT)


# ── A fake provider that records what would have been sent ──────────────────


class _Usage:
    input_tokens = 10
    output_tokens = 5
    cache_read_input_tokens = 0
    cache_creation_input_tokens = 0


class _Message:
    usage = _Usage()
    content = []
    stop_reason = "end_turn"


class _Stream:
    def __iter__(self):
        return iter(())

    def get_final_message(self):
        return _Message()


class _Messages:
    def __init__(self, sent: list, fail: bool = False):
        self.sent = sent
        self.fail = fail

    def create(self, **kwargs):
        self.sent.append(kwargs)
        if self.fail:
            raise RuntimeError("provider down")
        return _Message()

    @contextmanager
    def stream(self, **kwargs):
        self.sent.append(kwargs)
        yield _Stream()


class _Client:
    def __init__(self, sent: list, fail: bool = False):
        self.messages = _Messages(sent, fail)

    def with_options(self, **kwargs):
        # forma_core narrows retries per task (the safety classifier gets
        # none); the fake keeps recording on the same list.
        return self


@pytest.fixture
def provider(monkeypatch):
    """Every call's kwargs, and every forma_calls log, captured."""
    sent: list = []
    logged: list = []
    monkeypatch.setattr(forma_core, "_client", lambda: _Client(sent))
    monkeypatch.setattr(
        forma_core, "_log",
        lambda *a, **kw: logged.append({"task": a[1], **kw}),
    )
    return sent, logged


def _texts(system: list) -> list[str]:
    return [block["text"] for block in system]


def _breakpoints(system: list) -> int:
    return sum(1 for block in system if "cache_control" in block)


# ── Every task but the exempt three carries the law ──────────────────────────


def test_the_exempt_list_is_exactly_the_three_that_never_speak_to_a_rider():
    assert forma_core.SAFETY_LAW_EXEMPT_TASKS == EXEMPT
    assert EXEMPT <= set(forma_core.TASKS)


@pytest.mark.parametrize("task", LAW_TASKS)
def test_every_task_sends_the_safety_law_first(provider, task):
    sent, logged = provider
    forma_core.call(
        user_id="u1", task=task, system="You are the coach.",
        messages=[{"role": "user", "content": "hi"}], enforce_budget=False,
    )
    system = sent[-1]["system"]
    assert system[0]["text"].startswith(SAFETY_LAW)
    assert system[0]["text"].endswith("You are the coach.")
    # Merged into the one cached block: no extra breakpoint.
    assert _breakpoints(system) == 1
    assert logged[-1]["safety_law_version"] == SAFETY_LAW_VERSION


@pytest.mark.parametrize("task", LAW_TASKS)
def test_every_task_sends_the_safety_law_when_streaming(provider, task):
    sent, logged = provider
    with forma_core.stream(
        user_id="u1", task=task, system="You are the coach.",
        messages=[{"role": "user", "content": "hi"}], enforce_budget=False,
    ) as s:
        list(s)
    assert sent[-1]["system"][0]["text"].startswith(SAFETY_LAW)
    assert logged[-1]["safety_law_version"] == SAFETY_LAW_VERSION


@pytest.mark.parametrize("task", sorted(EXEMPT))
def test_the_exempt_tasks_go_without_it(provider, task):
    sent, logged = provider
    forma_core.call(
        user_id="u1", task=task, system="Name this thread.",
        messages=[{"role": "user", "content": "hi"}], enforce_budget=False,
    )
    assert all(SAFETY_LAW not in t for t in _texts(sent[-1]["system"]))
    assert logged[-1]["safety_law_version"] is None


def test_chat_block_lists_get_the_law_first_without_a_new_breakpoint(provider):
    """The chat service's [education, context, per-turn] blocks keep their own
    two breakpoints; the law sits inside the first cached prefix."""
    sent, _ = provider
    blocks = [
        {"type": "text", "text": "EDUCATION", "cache_control": {"type": "ephemeral"}},
        {"type": "text", "text": "CONTEXT", "cache_control": {"type": "ephemeral"}},
        {"type": "text", "text": "THIS TURN"},
    ]
    with forma_core.stream(
        user_id="u1", task="chat", system=blocks,
        messages=[{"role": "user", "content": "hi"}], enforce_budget=False,
    ) as s:
        list(s)
    system = sent[-1]["system"]
    assert system[0] == {"type": "text", "text": SAFETY_LAW}
    assert _texts(system)[1:] == ["EDUCATION", "CONTEXT", "THIS TURN"]
    assert _breakpoints(system) == 2 <= 4
    # The caller's own list is never mutated (the agentic loop reuses it).
    assert len(blocks) == 3


def test_the_real_chat_system_stays_within_the_breakpoint_limit(provider, db_session):
    from app.models.user import User

    user = User(email="sam@example.com", hashed_password="x", full_name="Sam")
    db_session.add(user)
    db_session.commit()
    sent, _ = provider
    system = coach_service._system_blocks(user, "CONTEXT", volatile="TURN")
    forma_core.call(
        user_id=user.id, task="chat_sync", system=system,
        messages=[{"role": "user", "content": "hi"}], enforce_budget=False,
    )
    sent_system = sent[-1]["system"]
    assert sent_system[0]["text"] == SAFETY_LAW
    assert _breakpoints(sent_system) <= 4


def test_a_failed_call_is_logged_with_the_version_too(monkeypatch):
    logged = []
    monkeypatch.setattr(forma_core, "_client", lambda: _Client([], fail=True))
    monkeypatch.setattr(
        forma_core, "_log", lambda *a, **kw: logged.append({"task": a[1], **kw})
    )
    with pytest.raises(RuntimeError):
        forma_core.call(
            user_id="u1", task="nudge", system="x",
            messages=[{"role": "user", "content": "hi"}], enforce_budget=False,
        )
    assert logged[-1]["error"] is True
    assert logged[-1]["safety_law_version"] == SAFETY_LAW_VERSION


def test_the_version_lands_on_the_forma_calls_row(monkeypatch):
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine)
    monkeypatch.setattr("app.database.SessionLocal", factory)

    forma_core._log("u1", "nudge", "dashboard", forma_core.HAIKU, _Usage(), 12,
                    safety_law_version=SAFETY_LAW_VERSION)
    forma_core._log("u1", "chat_title", "coach", forma_core.HAIKU, _Usage(), 12)

    db = factory()
    rows = {r.task: r.safety_law_version for r in db.query(FormaCall).all()}
    db.close()
    assert rows == {"nudge": SAFETY_LAW_VERSION, "chat_title": None}


def test_the_settings_version_matches_the_text_in_the_code():
    # forma_calls records the version of the text actually sent; settings
    # must never say otherwise.
    assert settings.safety_law_version == SAFETY_LAW_VERSION


def test_no_environment_variable_can_make_settings_name_another_law(monkeypatch):
    """One source of truth: settings reads the version from coach_skills, so
    a stale SAFETY_LAW_VERSION left on Railway can't split the record from
    the text (review finding 18: settings said safety-v1, the code safety-v3)."""
    from app.config import Settings

    monkeypatch.setenv("SAFETY_LAW_VERSION", "safety-v1")
    assert Settings().safety_law_version == SAFETY_LAW_VERSION
    monkeypatch.setattr(coach_skills, "SAFETY_LAW_VERSION", "safety-v99")
    assert settings.safety_law_version == "safety-v99"


# ── The law itself, and the lines that contradicted it ─────────────────────


def _no_dashes(text: str) -> bool:
    return not re.search(r"[–—]", text)


def test_the_law_carries_every_rule_and_the_numbers():
    for marker in (
        "## SAFETY LAW", "1. TRIAGE LADDER", "2. RED FLAGS", "3. HOW EVERY RED-FLAG",
        "4. CRISIS PROTOCOL", "5. HONESTY", "6. RESPONSIBILITY QUESTIONS", "7. HOLDS",
        "999", "112", "911", "NHS 111", "116 123", "85258", "988",
        "0808 801 0677", "ridewithforma.com/terms", "gareth@ridewithforma.com",
        "apply_safety_hold", "flag_for_review",
    ):
        assert marker in SAFETY_LAW, marker


@pytest.mark.parametrize("text", [
    SAFETY_LAW,
    coach_skills.compose_education(),
    coach_skills.distilled_persona(),
    coach_service.COACH_APP_PLAYBOOK,
    coach_service.ACTIVATION_PLAYBOOK,
    outreach_service.BRIEF,
    outreach_service.email_footer(),
    safety_screen.EMERGENCY_CARD,
    safety_screen.CRISIS_CARD,
    (Path(__file__).resolve().parents[1] / "app/core/product_knowledge.md").read_text(),
])
def test_copy_has_no_em_or_en_dashes_and_no_banned_words(text):
    assert _no_dashes(text)
    assert "kicker" not in text.lower()
    assert "injury-proof" not in text.lower()
    assert "prevents injury" not in text.lower()


def test_the_persona_no_longer_claims_to_have_coached_anyone():
    for text in (coach_skills.CORE_IDENTITY, coach_skills.DISTILLED_PERSONA):
        assert "coached at WorldTour level" not in text
        assert "you've coached" not in text.lower()
        assert "AI cycling coach" in text
    assert "world-class cycling coach" not in coach_skills.DISTILLED_PERSONA


def test_a_renamed_coach_is_still_an_ai_coach():
    assert "You are Coach Marco, Forma's AI cycling coach" in coach_skills.compose_education("Marco")
    assert "You are Coach Marco, Forma's AI cycling coach" in coach_skills.distilled_persona("Marco")


def test_the_direct_tone_hedges_on_health_and_never_tough_loves_safety():
    prompt = coach_skills.TONES["direct"]["prompt"]
    assert "except on health and safety, where you say what you don't know" in prompt
    assert "never on safety" in prompt


def test_the_outreach_email_owns_up_to_being_ai():
    assert "Never\nmention this is automated" not in outreach_service.BRIEF
    assert "never mention this is automated" not in outreach_service.BRIEF.lower()
    assert "say plainly that you are Forma's AI coach" in outreach_service.BRIEF
    assert "Never claim to be a\nperson" in outreach_service.BRIEF


def test_every_coach_email_ends_with_the_ai_footer(monkeypatch, db_session):
    from app.models.user import User

    user = User(email="sam@example.com", hashed_password="x", full_name="Sam")
    db_session.add(user)
    db_session.commit()

    class _Resp:
        content = [type("B", (), {"type": "text", "text": "Subject: Your next step\n\nSam, one thing."})()]

    monkeypatch.setattr(outreach_service.forma_core, "call", lambda **kw: _Resp())
    monkeypatch.setattr(outreach_service, "response_text", lambda r: r.content[0].text)
    state = {
        "quiet_days": 3, "stage": "goal",
        "next_action": {"title": "Set a goal", "instruction": "Goal, then Set a goal"},
    }
    subject, body = outreach_service.compose(db_session, user, state)
    assert subject == "Your next step"
    assert body.endswith(
        "Written by Forma, your AI coach. It can be wrong and isn't medical advice. "
        'Reply "stop" and the coach won\'t email you again.'
    )


def test_the_tightened_skill_lines_are_in_place():
    education = coach_skills.compose_education()
    assert "sharp pain, pain that is getting worse, or pain that" in education
    assert "Reframe training discomfort" in education
    assert "Reframe pain as information" not in education
    assert "never alone or after alcohol" in education
    assert "No fasted riding at all with diabetes, in pregnancy" in education
    assert "A re-test waits while a safety hold" in education
    assert "SAFETY LAW 2k decides the return" in education
    assert "never push for audacity or a bigger goal" in education
    assert "SAFETY LAW rule 4, every time" in education


def test_one_tsb_threshold_everywhere():
    threshold = f"TSB below {TSB_OVERTRAINING_THRESHOLD}"
    assert threshold in coach_service.COACH_APP_PLAYBOOK
    assert threshold in coach_skills.SKILLS["recovery"]
    assert "TSB < -30" not in coach_skills.compose_education()


def test_the_playbook_makes_safety_the_exception_to_asking_first():
    playbook = coach_service.COACH_APP_PLAYBOOK
    assert "call apply_safety_hold and flag_for_review straight away" in playbook
    assert "Medical questions go to a professional. On safety, decide conservatively." in playbook
    assert "only when `safety.allowed_intensity` is \"all\"" in playbook


def test_product_knowledge_answers_the_safety_questions():
    text = (Path(__file__).resolve().parents[1] / "app/core/product_knowledge.md").read_text()
    section = text[text.index("## Safety and responsibility"):text.index("## Who sees what")]
    for marker in (
        "no person checks them message by message",
        "It is not medical advice, and Forma is not an emergency service",
        "Settings, then Health",
        "I've been cleared",
        "This was a mistake",
        "Pause drops the trainer to light resistance",
        "Stop releases the resistance at once",
        "ridewithforma.com/terms",
        "gareth@ridewithforma.com",
    ):
        assert marker in section, marker
    assert "Health, safety and responsibility questions never fall back to this" in text


def test_the_law_carries_what_the_judges_asked_for():
    assert SAFETY_LAW_VERSION == "safety-v4"
    for marker in (
        # Numbers by country, and nothing from another country.
        "name only that\ncountry's numbers and services",
        "France 15 (SAMU)\n  or 112", "3114", "50808", "741741", "findahelpline.com",
        # Heart symptoms and fainting are seen where there's an ECG, never a GP slot.
        "Never offer a GP appointment as an equal option for those",
        'never "speak to your doctor"',
        "No riding\nof any kind, including easy spins, indoor sessions and commuting",
        "family history of sudden death before 50",
        "say in the same sentence that any yes means\ncalling the emergency number now",
        "Don't be alone for the next 24 hours, and don't drive.",
        "UK grassroots guidance after a suspected\nconcussion",
        "training hard\nwith a fever and a chest infection can put strain on the heart",
        "If you ride, keep it\ncompletely pain-free",
        "Don't skip, change or re-time\nyour medication to train",
        "Relative Energy\nDeficiency in Sport",
        "If food or weight is feeling hard to manage, Beat, the UK eating disorder "
        "charity, can help: 0808 801 0677 in England; other UK numbers at "
        "beateatingdisorders.org.uk.",
        "that's heatstroke: call 999 and cool them down while you wait.",
        "I don't recommend it, and I can't tell you you'll\nbe fine.",
        "No one at Forma reads chats as they happen, so please use these numbers now.",
        "If you have a health condition, symptoms or take medicine, check with\nyour GP before training.",
        "never mention the This was a mistake button",
        "Only say you've done something",
        "Describe symptoms only in the rider's words",
        # v4: the head injury signs the card lists, and the direct line when
        # the rider already gave one (red team #3); breathless at rest (2a).
        "slurred speech, trouble with vision or balance",
        "open with the conclusion, naming the sign\nin their words",
        "never a conditional list",
        "palpitations, breathlessness at rest, or\nbreathlessness out of proportion to effort",
    ):
        flat = " ".join(marker.split())
        assert flat in " ".join(SAFETY_LAW.split()), marker
    # The old example taught the coach that telling it in chat lifts a hold.
    assert "tell me when a doctor has cleared you" not in SAFETY_LAW
    assert "myocarditis" not in SAFETY_LAW


def test_the_v3_law_answers_the_question_first_and_closes_the_round_2_gaps():
    flat = " ".join(SAFETY_LAW.split())
    for marker in (
        # A plain answer before anything else.
        "Answer the rider's question first, in plain words.",
        '"Can I finish the set?" "No. Stop riding now."',
        # France sends settled chest pain to 15 or les urgences.
        "France 15 or les urgences, never the médecin traitant",
        # Injury: the pain-free rule is the limit, not the rider's call.
        "Until a physio has seen it, that's the limit.",
        # Beta blockers: no to chasing heart rate, and the emergency net.
        "a rider who wants to push harder to reach their zones hears no first",
        # Restriction: a dietitian for every rider, and Beat never bare.
        "every rider, UK included, to their GP and a registered sports dietitian",
        "Never give the number without that sentence.",
        # Heat: a solo rider tells someone.
        "heatstroke can stop them calling for help",
        # Layoff: the number of easy weeks named, and a timed warm-up.
        "or four weeks after three months or more off: name the one that applies",
        "a proper warm-up of 15 to 20 minutes",
        # Big jumps: why, then fuel, kit and the broom wagon.
        "about 60 g of carbohydrate an hour",
        "know where the broom wagon is",
        # Liability: no free-text opinion, either way.
        "no sentence of your own about who is or isn't responsible or liable",
        "don't take away any rights the law gives them",
        # Crisis lines.
        "SHOUT 85258 (UK)", "988lifeline.org",
    ):
        assert marker in flat, marker
    for gone in ("That's your call until a physio advises", "monitored around the clock",
                 "Beat: 0808"):
        assert gone not in flat, gone


def test_product_knowledge_never_offers_the_mistake_button_as_a_clearance():
    text = (Path(__file__).resolve().parents[1] / "app/core/product_knowledge.md").read_text()
    section = text[text.index("## Safety and responsibility"):text.index("## When the coach writes first")]
    assert "No one at Forma reads chats as they happen" in section
    assert "monitored around the clock" not in section
    assert "Some safety messages in chat are fixed text" in section
    assert "Saying anything in chat never lifts a hold" in section
    assert "the coach never suggests it" in section
    assert "A hold for being under 18 can't be lifted by the rider" in section
    assert "Never mention it in a crisis reply or to a rider who may be under 18" in section
