"""Transactional email, pluggable by configuration.

With POSTMARK_SERVER_TOKEN set, mail goes out through Postmark. Without it
(local dev, or production before the account exists), the full message is
logged instead, so every flow can be built and tested end to end before a
provider is wired in. Templates speak in Forma's voice: warm, direct,
British English, no em dashes.
"""

import logging

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
        "One click and your coach is ready",
        f"""{name},

Welcome to Forma. One click confirms this address is yours:

{link}

The link works for 24 hours. If you didn't create a Forma account, ignore
this and nothing happens.

See you on the road,
Forma
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
    greeting = f"{name.strip().split()[0]},\n\n" if name and name.strip() else ""
    place = (
        f"Your place is held. You're number {position} of a hundred.\n"
        if position
        else "Your place is held.\n"
    )
    return await send(
        to,
        "your place is held",
        f"""{greeting}{place}
The doors open on 15 September. Between now and then you'll get one letter a
week from me, and each will have something in it you can use on that week's
rides. Real numbers from my own testing, the marginal gains that cost nothing,
the fuelling maths most riders get wrong. If you're not a little faster by
launch day, I'll have failed at the easy half of this. The first lands this
week, and it's about the day I nearly put a bottle through Bradley Wiggins's
front wheel.

That's my end of it. Now the favour, and it's a real one.

Here's my guess about you, and do tell me if I've got it wrong. You're not lazy
and you're not short of information. You probably carry more data about your own
riding than anyone had access to twenty years ago. And somewhere behind you
there's a plan you stopped following, a block that came apart around week three,
and a quiet suspicion that the weak link was you.

It wasn't. It was almost certainly a plan written for a rider with no job, no
family and no February.

That's the thing I'm actually up against, and it's why I need you. I know what
went wrong in my own training. I can only guess at what goes wrong in yours.

So, one question, and I read every reply.

What frustrates you most about your training right now?

Not the tidy version. The real one. If it helps, the things I'm trying to
understand are:

  Where does it come apart? The week that goes sideways, the session that keeps
  moving down the calendar, the plan you quietly stopped opening.

  What actually stands between you and the thing you're aiming at? Time,
  knowledge, motivation, an old injury, a life that refuses to cooperate.

  What have you gone looking for in other apps and never found? Or found, and
  hated the way it worked.

One line is a complete answer. So is five paragraphs. There's no wrong thing to
say here, and you needn't be diplomatic about anything you've paid for.

What you tell me is what gets built between now and September. That isn't a
polite thing to say. It's genuinely how I decide what to work on next, and it's
the reason the list is a hundred people rather than a hundred thousand.

G

PS. When I say I read every reply, it's because the maths allows it. That won't
always be true, which is rather the point of going first.
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
        f"You're number {position} in the queue, and there are a hundred\nfounding places."
        if position
        else "There are a hundred founding places."
    )
    return await send(
        to,
        "I owe you an email",
        f"""{greeting}You put your name down for Forma {when}, and then I went quiet on
you. That's on me.

Here's what I was doing instead of writing to you. Building the thing. Forma
now reads your rides against the conditions you actually rode in, remembers
what you tell it, and rewrites next week when your life gets in the way. That
last part took longer than everything else put together.

So, the date. The doors open on 15 September.
{place}

From now until then you'll get one letter a week, and each one will have
something in it you can use on that week's rides. Real numbers from my own
testing, the marginal gains that cost nothing, the fuelling maths most riders
get wrong. If you're not a little faster by launch day, I'll have failed at
the easy half of this.

One question before any of that, and I do read every reply.

What does your current setup get wrong?

Not the feature you'd like added. The thing that actually annoys you. The plan
that assumed Tuesday evening was free when it never is. The app full of numbers
that never once told you what to do with any of them. Or the block that fell
apart in week three and somehow left you feeling like the problem was you.

Hit reply and tell me in a line. I'm still building this, and what riders tell
me now is what ends up getting built.

G

PS. You joined a list of one hundred, not one hundred thousand. When I write
that I read every reply, it's because the maths allows it.
""",
    )


async def send_password_reset(to: str, full_name: str | None, link: str) -> bool:
    name = _first_name(full_name, to)
    return await send(
        to,
        "Reset your Forma password",
        f"""{name},

Someone asked to reset the password on your Forma account. If that was
you, this link sets a new one:

{link}

It works for one hour. If it wasn't you, ignore this email; your password
stays as it is and your account is untouched.

Forma
""",
    )
