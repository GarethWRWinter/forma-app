"""Transactional email, pluggable by configuration.

With POSTMARK_SERVER_TOKEN set, mail goes out through Postmark. Without it
(local dev, or production before the account exists), the full message is
logged instead, so every flow can be built and tested end to end before a
provider is wired in. Templates speak in Forma's voice: warm, direct,
British English, no em dashes.
"""

import logging
import re
from datetime import datetime

import httpx

from app.config import settings

logger = logging.getLogger(__name__)

POSTMARK_API = "https://api.postmarkapp.com/email"


def is_configured() -> bool:
    return bool(settings.postmark_server_token)


async def send(
    to: str, subject: str, text_body: str, from_address: str | None = None
) -> bool:
    """Send one email. Returns True when handed to the provider (or logged in
    dev mode); False on provider failure.

    Everything sends from one address before launch, so from_address is a hook
    rather than something in use: the day there is a second sender, it is
    already here."""
    if not is_configured():
        logger.info(
            "EMAIL (no provider configured)\nFrom: %s\nTo: %s\nSubject: %s\n\n%s",
            from_address or settings.email_from, to, subject, text_body,
        )
        return True

    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            response = await client.post(
                POSTMARK_API,
                headers={
                    "X-Postmark-Server-Token": settings.postmark_server_token,
                    "Accept": "application/json",
                },
                json={
                    "From": from_address or settings.email_from,
                    "To": to,
                    "Subject": subject,
                    "TextBody": text_body,
                    "MessageStream": "outbound",
                },
            )
            response.raise_for_status()
        return True
    except httpx.HTTPError:
        logger.exception("Email send failed (to=%s, subject=%s)", to, subject)
        return False


def _first_name(full_name: str | None, email: str) -> str:
    return (full_name or email.split("@")[0]).split()[0]


async def send_verification(to: str, full_name: str | None, link: str) -> bool:
    name = _first_name(full_name, to)
    return await send(
        to,
        "One click to confirm your email",
        f"""{name},

Welcome to Forma. One click confirms this address is yours:

{link}

That way password resets can reach you, and so can I if you go quiet partway through setting up.

The link works for 24 hours. If you didn't create a Forma account, ignore this and nothing happens.

See you on the road,
Forma
""",
    )


# The safety panel above the boxes on the sign-up page, word for word.
SAFETY_PANEL_TEXT = (
    "Forma is an AI coach. It can be wrong, it isn't medical advice, and nobody "
    "watches you ride. If you have a heart condition, chest pain, fainting, a "
    "long-term condition, or you're pregnant, talk to your doctor first. Stop "
    "riding and get help if you feel chest pain, faintness, dizziness or unusual "
    "breathlessness."
)
# The 14-day rule, as section 4 of the terms words it.
REFUND_TEXT = (
    "If Forma isn't for you, cancel within 14 days of first subscribing, email "
    "gareth@ridewithforma.com, and I'll refund that first payment in full. No "
    "questions, no forms."
)
PRIVACY_URL = "https://ridewithforma.com/privacy"


def _long_date(when: datetime) -> str:
    return f"{when.day} {when:%B %Y}"


def plain_text(markdown: str) -> str:
    """A published legal document as it should read in a plain-text email:
    the words exactly as published, without the Markdown marks around them
    (bold stars and heading hashes), and with the source's hard line wraps
    joined up so paragraphs and list items reflow on a phone. Blank lines,
    list items and table rows keep their own lines."""
    lines: list[str] = []
    joinable = False  # whether the next wrapped line continues the last one
    for raw in markdown.strip().splitlines():
        line = re.sub(r"^#{1,6}\s+", "", raw.strip()).replace("**", "")
        if not line:
            lines.append("")
            joinable = False
        elif line.startswith(("- ", "|")) or raw.startswith("#"):
            lines.append(line)
            joinable = not line.startswith("|") and not raw.startswith("#")
        elif joinable:
            lines[-1] = f"{lines[-1]} {line}"
        else:
            lines.append(line)
            joinable = True
    return "\n".join(lines)


async def send_registration_welcome(
    to: str,
    full_name: str | None,
    link: str,
    terms_version: str,
    accepted_at: datetime,
    *,
    terms_text: str,
    privacy_version: str,
) -> bool:
    """The one email at sign-up: the confirm link first, then the rider's
    own record of what they agreed to. This is the durable-medium
    confirmation (CCRs reg 16, E-Commerce Regs reg 9(3)), so it carries the
    full text of the terms they accepted, read from the published file, not
    a link to a page that could change or go missing. Then the safety panel
    and the 14-day refund rule.

    Resend verification still uses send_verification: the record only needs
    to arrive once."""
    name = _first_name(full_name, to)
    return await send(
        to,
        "Welcome to Forma: confirm your email",
        f"""{name},

Welcome to Forma. One click confirms this address is yours:

{link}

The link works for 24 hours. If you didn't create a Forma account, ignore this and nothing happens.

Please keep this email. It's your record of what you agreed to when you joined.

What you agreed to
On {_long_date(accepted_at)} you accepted Forma's terms, version {terms_version}. The full terms are at the end of this email. You also agreed that Forma can use the health details you share with it to coach you, as privacy policy version {privacy_version} describes. The privacy policy explains how to withdraw that, and you can read it at {PRIVACY_URL}

Before you ride
{SAFETY_PANEL_TEXT}

Your first 14 days
{REFUND_TEXT}

See you on the road,
Gareth


The terms you accepted, version {terms_version}

{plain_text(terms_text)}
""",
    )


async def send_waitlist_welcome(
    to: str, name: str | None = None, position: int | None = None
) -> bool:
    """Letter 0. Fires on joining, and its real job is to earn a reply.

    Built on Compassionate Curiosity, in order and without skipping the first
    step, which is the one that is usually skipped:

    1. Acknowledge and validate. Before asking anything, name the thing the
       rider probably feels and has not said: that the last plan fell apart and
       they suspect the fault was theirs. Tell them it was not. Nobody answers
       an honest question while they are still braced to be judged.
       "Tell me if I have that wrong" keeps it an invitation, not a diagnosis,
       and a rider who corrects the guess has still replied.
    2. Get curious. One open question. The prompts under it are scaffolding,
       not homework, and the letter says out loud that one line will do.
    3. Joint problem solving. Not "tell me what to build for you" but the two
       of us against the same thing: plans written for a rider with no job and
       no February. They hold knowledge this cannot be built without.

    Deliberately a reply, never a form. A form is a company collecting; a reply
    is a person listening, and reading every one is the entire promise.
    """
    greeting = f"Hey, {name.strip().split()[0]}\n\n" if name and name.strip() else ""
    place = (
        f"Your place is held. You're number {position} in the queue for a hundred founding places.\n"
        if position
        else "Your place is held.\n"
    )
    return await send(
        to,
        "your place is held",
        f"""{greeting}{place}
The doors open soon, and I'll write the day they do. Between now and then you'll get one letter a week from me, and each will have something in it you can use on that week's rides. Real numbers from my own testing, the marginal gains that cost nothing, and what most riders get wrong. If you're not a little faster by launch day, I'll have failed at the easy half of this. The first lands this week, and it's about the day I nearly put a bottle through Sir Bradley Wiggins's front wheel.

That's my end of it. Now the favour, and it's a real one.

Here's my guess about you, and do tell me if I've got it wrong. You're not lazy and you're not short of information. You probably carry more data about your own riding than anyone had access to twenty years ago. And somewhere behind you there's a plan you stopped following, a block that came apart around week three, and a little voice in your head telling you that the weak link was you (that voice is lying to you, but we'll get to that another time).

It wasn't. It was almost certainly a plan written for a rider with no job, no family and no February.

That's the thing I'm actually up against, and it's why I need you. I know what went wrong in my own training. I can only guess at what goes wrong in yours.

So, one question, and I read every reply.

What frustrates you most about your training right now?

The real one, however untidy. If it helps, the things I'm trying to understand are:

Where does it come apart? The week that goes sideways, the session that keeps moving down the calendar, the plan you stopped opening and started avoiding.

What actually stands between you and the thing you're aiming at? Time, knowledge, motivation, an old injury, a life that refuses to cooperate.

What have you gone looking for in other apps and never found? Or found, and hated the way it worked.

One line is a complete answer. So is five paragraphs. There's no wrong thing to say here, and you needn't be diplomatic about anything you've paid for.

What you tell me is what gets built between now and launch. I mean that literally. It's how I decide what to work on next, and it's the reason there are a hundred founding places rather than a hundred thousand.

G

PS. When I say I read every reply, it's because the maths allows it. That won't always be true, which is rather the point of going first.
""",
    )


async def send_waitlist_reintroduction(
    to: str, name: str | None = None, position: int | None = None,
    joined_month: str | None = None,
) -> bool:
    """For the riders who joined before the letters existed and then heard
    nothing for weeks.

    They cannot get the standard Letter 0: "your place is held" reads as
    nonsense to someone who held it in July and has had silence since. So this
    one opens by owning the gap, because the alternative is a rider deciding
    the whole thing went quiet on them twice.
    """
    greeting = f"{name.strip().split()[0]},\n\n" if name and name.strip() else ""
    when = f"back in {joined_month}" if joined_month else "a few weeks ago"
    place = (
        f"You're number {position} in the queue, and there are a hundred founding places."
        if position
        else "There are a hundred founding places."
    )
    return await send(
        to,
        "I owe you an email",
        f"""{greeting}You put your name down for Forma {when}, and then I went quiet on you. That's on me.

Here's what I was doing instead of writing to you: building the thing. Forma now reads your rides against the conditions you actually rode in, remembers what you tell it, and rewrites next week when your life gets in the way. That last part took longer than everything else put together.

So, the date. I won't promise one yet; you'll get a letter from me the day the doors open. {place}

From now until then you'll get one letter a week, and each one will have something in it you can use on that week's rides. Real numbers from my own testing, the marginal gains that cost nothing, the fuelling maths most riders get wrong. If you're not a little faster by launch day, I'll have failed at the easy half of this.

One question before any of that, and I do read every reply.

What does your current setup get wrong?

Not the feature you'd like added. The thing that actually annoys you: the plan that assumed Tuesday evening was free when it never is, the app full of numbers that never once told you what to do with any of them, or the block that fell apart in week three and somehow left you feeling like the problem was you.

Hit reply and tell me in a line. I'm still building this, and what riders tell me now is what ends up getting built.

G

PS. There are a hundred founding places, not a hundred thousand. When I write that I read every reply, it's because the maths allows it.
""",
    )


async def send_wahoo_disconnected(
    to: str, name: str | None = None, reason: str | None = None
) -> bool:
    """Sent the moment a Wahoo connection dies, not days later.

    Rotating refresh tokens die occasionally and no amount of care fully
    prevents it. What is preventable is silence: the first time this happened
    it went unnoticed for four days, and the only signal was a badge in
    Settings nobody had a reason to look at.

    Two variants, because the fix differs. A dead refresh token is repaired by
    Reconnect. The token cap ("token_cap") is not: Wahoo allows an app ten
    keys per rider, Reconnect would ask for an eleventh and be refused, and
    the only way through is the rider removing Forma from their Wahoo account
    first. Sending them to Reconnect in that state is a loop with no exit.
    """
    greeting = f"{name.strip().split()[0]},\n\n" if name and name.strip() else ""
    if reason == "token_cap":
        return await send(
            to,
            "Wahoo has stopped talking to Forma",
            f"""{greeting}Your Wahoo connection has stopped working, and this time Reconnect on its own won't fix it. Wahoo allows an app ten keys per rider and Forma has used them all, which is a fault on my side, not yours.

Clearing it takes about a minute, in this order:

1. In the Wahoo app: Settings, then Authorized Apps, then Forma, then Deauthorize. (Or sign in at wahooligan.com/profile and remove Forma there.)

2. Back in Forma: Settings, then Data in, then Reconnect on the Wahoo card.

Nothing is lost. Wahoo still has every ride, and I'll fetch the ones I missed as soon as you reconnect.

Forma
""",
        )
    return await send(
        to,
        "Wahoo has stopped talking to Forma",
        f"""{greeting}Your Wahoo connection just stopped working, so your rides aren't reaching me at the moment.

Nothing is lost. Wahoo still has every ride, and I'll fetch the ones I missed as soon as you reconnect. It takes about twenty seconds: Settings, then Data in, then Reconnect on the Wahoo card.

This happens occasionally because Wahoo issues a new key each time we talk, and very rarely one goes astray. It isn't something you did, and it isn't something your head unit did.

Forma
""",
    )


async def send_password_reset(to: str, full_name: str | None, link: str) -> bool:
    name = _first_name(full_name, to)
    return await send(
        to,
        "Reset your Forma password",
        f"""{name},

Someone asked to reset the password on your Forma account. If that was you, this link sets a new one:

{link}

It works for one hour. If it wasn't you, ignore this email; your password stays as it is and your account is untouched.

Forma
""",
    )


SAFETY_ALERT_EXCERPT_CHARS = 500

# Failures that hit every rider, not one red flag: their own subject and next
# step, and never a rider's words (the excerpt says what failed).
_OPS_ALERTS = {
    "reply_failed_outage": (
        "Forma ops alert: coach replies are failing",
        "Check the Anthropic status page and the Railway logs. Riders with a red "
        "flag still get the fixed safety reply.",
    ),
    "reply_failed_credit": (
        "Forma ops alert: the Anthropic credit balance is empty",
        "Top up the Anthropic account. Until then every coach reply fails; riders "
        "with a red flag still get the fixed safety reply.",
    ),
}


async def send_safety_alert(kind: str, user_email: str, user_id: str, excerpt: str) -> bool:
    """Tell Gareth a crisis or minor red flag fired (safeguarding protocol).

    Plain and internal: who, what matched, and a short excerpt, never the
    whole conversation. He reads the rest in the app and checks the coach's
    reply within 24 hours."""
    excerpt = (excerpt or "").strip()
    if len(excerpt) > SAFETY_ALERT_EXCERPT_CHARS:
        excerpt = excerpt[: SAFETY_ALERT_EXCERPT_CHARS - 3].rstrip() + "..."
    if kind in _OPS_ALERTS:
        subject, todo = _OPS_ALERTS[kind]
        return await send(
            settings.founder_alert_email,
            subject,
            f"""Coach replies are failing.

{excerpt or "(no detail)"}

Last rider affected: {user_id}

{todo}
""",
        )
    if kind == "invite_abuse":
        # Not a rider's red flag and not a failing reply: someone is guessing
        # invite codes at sign-up. The excerpt says how many tries, from how
        # many addresses, and what to do.
        return await send(
            settings.founder_alert_email,
            "Forma ops alert: someone is guessing invite codes",
            f"""{excerpt or "(no detail)"}

Check the Railway logs for "Invite guessing alarm".
""",
        )
    if kind == "reply_failed":
        return await send(
            settings.founder_alert_email,
            "Forma safety alert: a red-flag reply failed",
            f"""The coach's reply failed on a turn that raised a red flag.

Rider id: {user_id}
Rider email: {user_email}

What happened:
{excerpt or "(no detail)"}

Check the conversation within 24 hours (safeguarding protocol).
""",
        )
    return await send(
        settings.founder_alert_email,
        f"Forma safety alert: {kind}",
        f"""A safety red flag fired in the coach chat.

Kind: {kind}
Rider id: {user_id}
Rider email: {user_email}

What they wrote:
{excerpt or "(no text)"}

Check the conversation and the coach's reply within 24 hours (safeguarding protocol).
""",
    )
