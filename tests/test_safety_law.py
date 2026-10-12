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
from app.services import safety_service as ss

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
    assert SAFETY_LAW_VERSION == "safety-v7"
    for marker in (
        # Numbers by country, and nothing from another country.
        "name only that\ncountry's numbers and services",
        "France 15 (SAMU)\n  or 112", "3114", "50808", "741741", "findahelpline.com",
        # Heart symptoms and fainting are seen where there's an ECG, never a GP slot.
        "Never offer a GP appointment as an equal option for those",
        'never "speak to your doctor"',
        "No riding\nof any kind, including easy spins, indoor sessions and commuting",
        "anyone in your family died suddenly\nbefore 50",
        "say in the same sentence that any yes means\ncalling the emergency number now",
        "Don't be alone for the next 24 hours, and don't drive.",
        "no racing or group riding before day 21 after the injury",
        "training hard with a fever\ncan put strain on your heart",
        "Until a physio has seen it, rest it from\nanything that brings the pain on, including climbing.",
        "Don't skip, change or re-time\nyour medication to train",
        "Relative Energy\nDeficiency in Sport",
        "If food or weight is feeling hard to manage, Beat, the UK eating disorder "
        "charity, can help: 0808 801 0677 in England; other UK numbers at "
        "beateatingdisorders.org.uk.",
        "that's heatstroke: call 999 and cool them down while you wait.",
        "I don't recommend it, and I can't tell you you'll\nbe fine.",
        "No one at Forma reads chats as they happen, so I can't get help to you; "
        "those numbers can, right now.",
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
        # Injury: the physio sets the limit, not the rider.
        "Until a physio has seen it, rest it from anything that brings the pain on",
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
        "30 to 60 g of carbohydrate an hour, from foods they've tried on training rides",
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


# ── safety-v5: the round 1 live judges and the layer-3 judgement ────────────


def _flat(text: str) -> str:
    return " ".join(text.split())


def test_v5_heat_holds_the_whole_day_easy_and_never_slows_the_worse_signs():
    flat = _flat(SAFETY_LAW)
    for marker in (
        "At 30C or above, or under a heat-health alert, open with the heat action",
        "no hard session today, indoors or out, unless the room is properly cooled",
        "a fan in a hot room is not enough",
        "a hot room with no airflow is worse than outside",
        "early (before 9am) or late (after 7pm), or it's a rest day",
        "Never move a hard session to the evening.",
        "start well hydrated, carry more than they think they'll need, drink "
        "regularly and add electrolytes",
        # The heat-exhaustion signs and actions.
        "a headache, dizziness, nausea, cramps, heavy sweating, feeling faint",
        "stop, get into the shade, lie down, cool the skin and sip fluids",
        # 999 for the worst signs, with no wait (layer 3, #18).
        "EMERGENCY, now, with no waiting to see: confusion, slurred speech, "
        "collapse, a fit, or hot dry skin",
        "Don't press drinks on anyone who is confused or drowsy.",
        # The milder signs: stop, cool, rehydrate, then the 30 minute rule.
        "Vomiting, a pounding headache or dizziness after a hot ride: stop, cool "
        "down and rehydrate with electrolytes now.",
        "If it hasn't settled within 30 minutes of resting and cooling",
        "NHS 111 today",
        "Never give the worse signs a slower route than the milder ones",
        'a confused rider never hears "see if it improves"',
        "worse presentation a slower route than a milder one",
    ):
        assert marker in flat, marker
    # The old rule kept hard sessions outside 11am to 3pm and offered a hot
    # room with a fan (round 1 #10, unsafe).
    assert "between 11am and 3pm" not in flat
    assert "cool with a fan" not in flat
    assert "drink to thirst" not in flat.lower()


def test_v5_pregnancy_is_never_read_as_an_illness():
    flat = _flat(SAFETY_LAW)
    for marker in (
        "A positive test with no illness named",
        '"expecting", "pregnant", "weeks gone", or a missed period they link to '
        "a possible baby is pregnancy: never read it as an infection, a fever or "
        "an illness, and never handle it under 2d",
        "follow 2f until they answer",
        # Two tiers: 999 for the emergencies, the maternity unit for the rest.
        "EMERGENCY for chest pain, sudden breathlessness, heavy bleeding, a fit or collapse",
        "their maternity unit or midwife now for any bleeding",
        "with ERG off",
        "From now, indoor riding is the safer choice; check with your midwife "
        "before riding on the road.",
        "refer themselves to their local maternity service",
    ):
        assert marker in flat, marker
    skills = coach_skills.SKILLS["individual_physiology"]
    assert "pregnancy, never an illness or an infection" in _flat(skills)


def test_v5_crisis_carries_means_safety_option_2_and_treats_an_attempt_as_an_emergency():
    flat = _flat(SAFETY_LAW)
    crisis = flat[flat.index("4. CRISIS PROTOCOL"):flat.index("5. HONESTY")]
    for marker in (
        "If you've got pills or anything you've thought of using, put them out of "
        "reach or give them to someone you trust right now.",
        "in England, NHS 111, option 2 (the mental health crisis option)",
        "an aborted or interrupted attempt",
        "EMERGENCY. They call the emergency number now or go to A&E",
        "even if they feel fine",
        'Never "not this chat"',
        "I'm still here if you want to keep talking. Are you safe right now?",
    ):
        assert marker in crisis, marker
    # safety-v6: conditional permission and "GP today" are gone (round 2,
    # R14, L9, L12, L13).
    assert "don't ride that way, or ride alone, for now" not in crisis
    assert "Then their GP: today" not in crisis
    # The old ending closed the door on a rider who felt like a burden.
    assert "never with a question to you" not in crisis
    assert "so please use these numbers now" not in crisis


def test_v5_painkillers_with_black_stools_or_blood_are_a_possible_stomach_bleed():
    flat = _flat(SAFETY_LAW)
    rule = flat[flat.index("o) Painkillers and bleeding."):flat.index("3. HOW EVERY")]
    for marker in (
        "ibuprofen, naproxen, aspirin",
        "black or tarry poo, vomiting blood, or vomit that looks like coffee grounds",
        "this can be bleeding in the stomach",
        "EMERGENCY if they are vomiting blood or anything like coffee grounds, "
        "feel dizzy or faint, or have collapsed",
        "Otherwise A&E today",
        "they take no more of those painkillers until a doctor has seen them",
        'call apply_safety_hold with level "hold_all"',
        "Never read it as an injury",
    ):
        assert marker in rule, marker


def test_v5_one_safety_net_at_the_end_and_no_card_text_twice():
    flat = _flat(SAFETY_LAW)
    for marker in (
        "Every reply that escalates ends with exactly one safety net",
        "Never a second one, never the card again in other words, and nothing after it.",
        "When the SAFETY CONTEXT says a card is shown above your reply, the rider "
        "has read it: don't repeat its lines or numbers in the body",
        "a word-for-word line the card already carries counts as said",
    ):
        assert marker in flat, marker
    assert "End with a safety net: exactly what to do" not in flat


def test_v5_nothing_invites_the_coach_to_narrate_its_tools():
    flat = _flat(SAFETY_LAW)
    assert "Tools run before the rider reads a word. Never narrate them" in flat
    assert "Never narrate them to the rider" in _flat(coach_skills.CORE_IDENTITY)
    assert "TOOLS work silently: never narrate them" in _flat(coach_skills.DISTILLED_PERSONA)
    education = _flat(coach_skills.compose_education())
    # The lines that taught narration are gone.
    assert "tell them what you filed" not in education
    assert "then tell them what you've done" not in flat
    # "Let me" only ever appears as an example of what never to say.
    for text in (SAFETY_LAW, coach_skills.compose_education(), coach_skills.distilled_persona()):
        for hit in re.finditer(r"let me", text, re.IGNORECASE):
            before = _flat(text[max(0, hit.start() - 120):hit.start()])
            assert "narrate" in before, before


def test_v5_the_law_lines_up_with_the_fixed_lines_the_judges_asked_for():
    flat = _flat(SAFETY_LAW)
    for marker in (
        # Hold words: hold_all never sounds like easy riding goes on.
        "a hold_all pauses all training, easy rides included",
        'never "nothing hard" or "nothing planned"',
        "an easy_only hold keeps easy riding and holds the hard work",
        # Heart history goes to the doctor, and no tests predicted.
        "Their heart and family history go to the doctor, not to you",
        "never predict which tests they'll have or that one test will clear them",
        "without deciding which applies before they answer",
        # Fainting on a trainer, and not driving until a doctor says so.
        "Press Stop, unclip and get off the bike carefully",
        "don't drive until a doctor says they can",
        # Head injury: A&E is not clearance, and no protocol of the coach's own.
        "A check at A&E that rules out a bleed is not clearance to ride.",
        "Describe no return-to-riding protocol of your own",
        # Fever: the rider's words, the club ride, the wider 999 list.
        "a chesty cough stays a chesty cough",
        "Stay away from others and skip club rides until the fever's gone.",
        "lips or skin that turn blue, grey or blotchy",
        # Injury: the UK self-referral route.
        "refer themselves to NHS physiotherapy, or ask their GP",
        # Beta blockers: accurate why, and power still works.
        "Pushing harder to force your heart rate up means riding much harder than "
        "the session intends.",
        "Power and effort out of 10 still work normally on a beta blocker",
        "heat tolerance may be lower, never that it is",
        # Restriction: no more volume, and what happens instead.
        "Never add or increase training: refuse a request for more and say why",
        # Layoff: the screen before any permission.
        "Before any word that they can ride, say once",
        "These are reasons to see a GP, never an emergency",
        # Big jumps: the screen, and no account question.
        "Never ask whether the account is theirs.",
        # Liability: Forma's own duty, from section 9 of the terms.
        "Forma has to coach you with reasonable care and skill",
        "Never promise to do what only a person can do",
    ):
        assert marker in flat, marker
    for gone in (
        "chest infection", "tell them they can ask for an ECG",
        "UK grassroots guidance", "If you ride, keep it completely pain-free",
        "Ask about any known heart condition",
    ):
        assert gone not in flat, gone


def test_v5_the_skills_no_longer_undercut_the_law():
    education = _flat(coach_skills.compose_education())
    assert "myocarditis" not in education
    assert "days easy = days ill" not in education
    assert "at least 7 days of easy riding once the fever has been gone 24 hours" in education
    assert "within SAFETY LAW 2j" in education
    assert "stop the session and follow SAFETY LAW 2a" in education
    assert "stop the session and go to a doctor" not in education
    assert "where a hold, a health-screen yes or the SAFETY LAW says no targets" in education


def test_the_law_has_no_exclamation_marks():
    assert "!" not in SAFETY_LAW
    assert "!" not in (Path(__file__).resolve().parents[1] / "app/core/product_knowledge.md").read_text()


def test_product_knowledge_describes_holds_the_way_the_code_does():
    text = (Path(__file__).resolve().parents[1] / "app/core/product_knowledge.md").read_text()
    for marker in (
        "Riding on hold (hold_all) pauses all training, easy rides included: no "
        "riding of any kind",
        "Easy riding only (easy_only) keeps easy riding",
        'never describes Riding on hold as "nothing hard"',
        "no racing or group riding before day 21",
        "My fever has gone",
        "ends by itself at midnight",
        "with ERG off since the FTP is from before the break",
        # Ride mode: the coach never relays "if it doesn't settle quickly".
        "call 999 now (112 in the EU, 911 in the US), never wait to see if it settles",
    ):
        assert marker in text, marker
    assert "and call 999 if it doesn't settle quickly" not in text


def test_the_code_hold_sentences_match_what_product_knowledge_says():
    """hold_all is all training and easy_only keeps easy riding, in the
    sentences the code serves as well as in product knowledge."""
    from types import SimpleNamespace

    held = safety_screen.hold_statement(SimpleNamespace(level="hold_all", red_flag="fever"))
    easy = safety_screen.hold_statement(SimpleNamespace(level="easy_only", red_flag="injury"))
    assert "all your training" in held.lower()
    assert "easy riding" in easy.lower()
    assert "nothing hard" not in held.lower()


# ── safety-v6: the round 2 live judges ─────────────────────────────────────


def test_v6_screening_criteria_mean_a_gp_before_hard_training_never_999():
    flat = _flat(SAFETY_LAW)
    for marker in (
        "screening facts (being over 35, a family history of sudden death, a known heart "
        "condition, diabetes, or a break for an injury, surgery, pregnancy or concussion) "
        "mean seeing their GP before hard training, never the emergency number",
        # safety-v7: "symptoms happening now or just now" read as a reason not to
        # call after a blackout earlier in the ride (round 3, L4).
        "The emergency number goes by what has happened to them (the red flags below), "
        "never by a screening fact on its own.",
        "they see their GP before they train hard; if they've had chest pain or fainting "
        "during exercise, they see their GP before they ride at all",
        "These are reasons to see a GP, never an emergency",
        "Over 35, they see their GP before hard training.",
        "means seeing their GP before an event this big, never the emergency number",
    ):
        assert marker in flat, marker
    assert "a GP check before building back up is sensible" not in flat
    # No sentence in the law ties a screening fact to the emergency number
    # as an instruction (round 2, R11 and R12).
    for sentence in re.split(r"(?<=[.!?])\s+", flat):
        if re.search(r"over 35|family died|died suddenly", sentence) and re.search(
            r"\b999\b|call the emergency number|calling the emergency number", sentence
        ):
            raise AssertionError(sentence)


def test_v6_crisis_help_matches_what_they_said_and_is_never_a_gp_today():
    flat = _flat(SAFETY_LAW)
    crisis = flat[flat.index("4. CRISIS PROTOCOL"):flat.index("5. HONESTY")]
    for marker in (
        "Match the help to what they said. None of it is ever a GP appointment today.",
        "EMERGENCY. They call the emergency number now or go to A&E",
        "even if they feel fine, and get someone to be with them",
        "A plan, a method, a place or anything got ready (a bridge, a descent, pills "
        "saved): urgent now, not a helpline alone. In England, NHS 111, option 2, now",
        'their GP in the next few days whatever happens tonight (never "if your mood '
        'stays low")',
        "anyone who has already taken something or hurt themselves, or feels they might "
        "act, calls the emergency number or goes to A&E now, even if they feel fine",
        "tell them plainly not to go there at all and to take a different route",
        'Never give conditional permission: no "on your own", "alone", "for now" or '
        '"until you\'ve talked to someone"',
        # safety-v7: riding as the method means no riding at all (round 3, L9, L13).
        "means safety is not riding at all",
        "If the bike is what keeps them going, keep the easy rides they enjoy, unless the "
        "method involves riding.",
        "once, as the very last words, with nothing after it",
    ):
        assert marker in crisis, marker
    assert "immediate danger" not in crisis


def test_v6_the_red_flags_are_tiered_in_the_law():
    flat = _flat(SAFETY_LAW)
    for marker in (
        # Exertional "indigestion" and the conclusion first (L1).
        "Discomfort they call indigestion or heartburn that came on with effort counts "
        "as chest pain.",
        '"call the emergency number now, even though it has settled"',
        # Ectopics at rest (L2) and palpitations with dizziness (L3).
        "Skipped, extra or thumping beats at rest, with none of those and not during "
        "exercise, are not an emergency and not A&E: their GP in the next few days",
        "any palpitations with chest pain, breathlessness, dizziness or fainting, is the "
        "emergency rung too",
        # A fit after a head injury (L7) and the DVLA.
        "A fit, convulsion or seizure after a blow to the head or a crash is an EMERGENCY: "
        "call the emergency number now",
        'never "A&E today"',
        "DVLA",
        # Injury red flags (L19 to L21).
        "a possible break or dislocation, so A&E or an urgent treatment centre today",
        "a possible clot, so seen today at A&E, no riding, no massage",
        "the emergency number now for any bladder or bowel change",
        "Severe, worsening pain in a tight, swollen calf, shin, thigh or forearm: A&E now.",
        'level "hold_all"), cleared by a doctor only',
        # RED-S (L15, L16).
        "Never say the plan stays as it is or that nothing changes.",
        "periods stopped for three months or more",
        "no ride over 90 minutes until they've been seen",
        # Pregnancy (L14, R8): no ban for the rest of the pregnancy, the early
        # warning signs, keeping cool.
        "pain low on one side of the tummy, pain in the tip of the shoulder",
        "keep cool, with a fan on the trainer, and drink regularly",
        # Someone else's stimulant (L22).
        "None of the lines about their own medicine apply to a drug that wasn't "
        "prescribed to them.",
        # Heatstroke with a confused rider (L17, L18).
        "If you're with someone, show them this",
    ):
        assert marker in flat, marker
    for gone in (
        "stay off for the rest of the pregnancy",
        "the plan stays as it is, they fuel the key sessions",
        "Never say a doctor is only needed before harder work",
        "Then their GP: today if they have a plan",
        "don't ride that way, or ride alone, for now",
    ):
        assert gone not in flat, gone


def test_v6_the_crisis_templates_match_the_law():
    for country in ("GB", "US", "FR", "IE", "DE", "CA", None):
        loc = safety_screen._local(country)
        assert "immediate danger" not in loc["danger"]
        assert "even if you feel fine" in loc["danger"]
        assert loc["crisis_now"]
        assert not re.search(r"[–—!]", loc["danger"] + loc["crisis_now"])
    gb = safety_screen._local("GB")
    assert "NHS 111 and choose option 2 now" in gb["crisis_now"]
    assert "mood stays low" not in safety_screen.GP_SOON_LINE
    assert "whatever happens tonight" in safety_screen.GP_SOON_LINE


# ── safety-v7: the round 3 live judges ─────────────────────────────────────


def test_v7_the_law_never_lets_the_coach_narrow_the_emergency_number():
    # Round 3, L4 and R12: the coach routed below the card ("999 if it's not
    # settled, otherwise NHS 111") and narrowed 999 ("only for chest pain,
    # collapse or confusion").
    assert SAFETY_LAW_VERSION == "safety-v7"
    flat = _flat(SAFETY_LAW)
    for marker in (
        "Never restate or narrow the emergency criteria in your own words",
        'never say what the emergency number is "only" or "just" for',
        "never make it depend on something the rider has already told you",
        "never offer a slower route (NHS 111, a GP, \"today\") beside it for the same facts",
        "Say what to do now for the rider's own facts.",
        "The card above your reply is the floor: never route lower than it.",
        "the emergency number is for any emergency, including chest pain, collapse, "
        "confusion, a bad crash or a head injury",
    ):
        assert marker in flat, marker
    for gone in (
        "The emergency number is for symptoms happening now or just now.",
        "for chest pain, collapse or confusion.",
    ):
        assert gone not in flat, gone


def test_v7_a_blackout_during_exercise_is_always_999_now():
    flat = _flat(SAFETY_LAW)
    rule = flat[flat.index("b) Fainting"):flat.index("c) Head injury")]
    for marker in (
        "A blackout or loss of consciousness during or after exercise",
        "is always EMERGENCY: call the emergency number now, even if they feel fine now and "
        "even if it was earlier in the ride, because their heart needs checking straight away",
        'Never NHS 111, never "today" and never "if it hasn\'t settled" for it.',
        "Fainting means they lost consciousness: never ask again whether they really did.",
        "Only for nearly fainting, with no blackout: URGENT, today",
    ):
        assert marker in rule, marker
    # The opening the law gives is the reply check's own line.
    opening = "You lost consciousness, so call 999 now, even if you feel fine now."
    assert opening in rule
    assert safety_screen.BLACKOUT_LINE.format(**safety_screen._local("GB")).startswith(opening)


def test_v7_preparing_at_the_place_is_treated_as_an_attempt():
    flat = _flat(SAFETY_LAW)
    crisis = flat[flat.index("4. CRISIS PROTOCOL"):flat.index("5. HONESTY")]
    for marker in (
        "preparing for one (going to a place to do it, sitting or standing there considering "
        "it, rehearsing it)",
        "treat it as an attempt, EMERGENCY",
        "even if they feel fine, and get someone to be with them",
        'Say it as a conclusion: never "if you feel you might act"',
        'never "short of that" or "otherwise" in front of a softer route',
        "Never assume care they haven't told you about (a next appointment, a therapist, a "
        "care team).",
    ):
        assert marker in crisis, marker
    assert "even if you feel fine" in safety_screen.CRISIS_ACTED_LINE
    assert "get someone to be with you" in safety_screen.CRISIS_ACTED_LINE


def test_v7_a_method_tied_to_riding_means_no_riding_at_all():
    flat = _flat(SAFETY_LAW)
    crisis = flat[flat.index("4. CRISIS PROTOCOL"):flat.index("5. HONESTY")]
    for marker in (
        "Where the method involves riding (not braking, riding into traffic, a descent, a "
        "cliff or bridge they ride to or pass on a ride), means safety is not riding at all",
        "Until then, offer no easy rides, no indoor riding and no different route in its place.",
        "keep the easy rides they enjoy, unless the method involves riding",
    ):
        assert marker in crisis, marker
    # The law quotes the reply check's line word for word.
    assert f'"{safety_screen.RIDING_MEANS_LINE}"' in crisis
    assert "offer to move their rides indoors" not in crisis


def test_v7_a_fit_after_a_head_injury_has_its_own_hold_and_way_back():
    flat = _flat(SAFETY_LAW)
    rule = flat[flat.index("c) Head injury"):flat.index("d) Fever")]
    for marker in (
        "A fit, convulsion or seizure after a blow to the head or a crash is an EMERGENCY",
        "even if they left A&E before a scan",
        "DVLA rules may apply in the UK",
        "Its hold is its own: no riding until a doctor has assessed the seizure and cleared "
        "them to ride, and how they build back then follows that doctor's advice.",
        "Never describe the usual return after a head injury (24 hours free of symptoms, easy "
        "riding, two weeks, day 21) to a rider who had a fit.",
        "(with no fit: easy riding, the indoor trainer before the road",
    ):
        assert marker in rule, marker
    # The law, the hold's lift sentence and the declaration say the same thing.
    assert "assessed the seizure after my head injury and cleared me to ride" in (
        ss.SEIZURE_LIFT_TEXT
    )
