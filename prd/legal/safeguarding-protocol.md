# Safeguarding protocol

Internal, not published. Version 2, 8 October 2026. For Gareth. Not legal
advice; have the solicitor read it with the terms.

**What this covers.** Forma emails me at gareth@ridewithforma.com when the
coach chat hits a crisis red flag (hopelessness, self-harm, suicide) or a
minor red flag (someone may be under 18), when the coach's reply fails on a
turn with an urgent red flag, and when coach replies fail for everyone. Every
alert about a rider is a row in `safety_events`, and I check every one within
24 hours of it arriving.

## The alerts

| Subject | What happened | Section |
|---|---|---|
| Forma safety alert: crisis | The check or the coach flagged hopelessness, self-harm or suicide. | Crisis |
| Forma safety alert: minor | The rider said they're under 18, or the coach judged they are. The account is on hold and renewal is stopped. | Minor |
| Forma safety alert: a red-flag reply failed | The model failed on a turn with an urgent red flag, so the rider got the fixed safety reply. | A red-flag reply failed |
| Forma ops alert: the Anthropic credit balance is empty | Every coach reply fails until the account is topped up. | Credit balance empty |
| Forma ops alert: coach replies are failing | More than three replies failed across all riders inside ten minutes. | Replies failing for everyone |

One incident sends one email. A second crisis or minor flag for the same
rider inside 30 minutes doesn't email again, a failed red-flag reply emails
at most once per rider per half hour, and each ops alert at most once an
hour while it lasts.

## Three rules that never bend

1. **I never give personal health advice.** Not by email, phone or DM. No
   counselling, no reassurance about symptoms, no "you'll be fine". I'm not a
   clinician, and the company only protects me while I act for it.
2. **I never promise that anyone at Forma will check in.** The coach is told
   the same. Point people to people who can help, not to us.
3. **Danger right now means 999.** If what I read suggests someone is in
   danger now (a plan, a method, a time) and I can identify them, I call 999
   and pass on what I know. The law allows sharing information to protect
   someone's life (UK GDPR Article 9(2)(c)). I don't try to handle it myself.

## The review tool

`scripts/review_safety_event.py` reads and writes the safety record. It is a
dry run until `--commit`: without it, it prints what it would do and writes
nothing, in the database or in Stripe. It never makes a refund; it prints the
steps and I make them in the Stripe dashboard. I run it from my Mac, in the
repo, against production. Agents never run it.

**The list and marking an event reviewed need only the database:**

```
cd ~/gareth-coaching-api

# Unreviewed events, newest first, then every open hold only Forma can lift
railway run --service Postgres bash -c 'DATABASE_URL="$DATABASE_PUBLIC_URL" .venv/bin/python scripts/review_safety_event.py'

# The same, with failed ordinary replies listed too, and up to 200 events
railway run --service Postgres bash -c 'DATABASE_URL="$DATABASE_PUBLIC_URL" .venv/bin/python scripts/review_safety_event.py --include-failures --limit 200'

# Mark one event reviewed (drop --commit for a dry run)
railway run --service Postgres bash -c 'DATABASE_URL="$DATABASE_PUBLIC_URL" .venv/bin/python scripts/review_safety_event.py --reviewed EVENT_ID --note "Read it, reply was right" --commit'
```

**Lifting a hold and closing an account also talk to Stripe.** The Postgres
service doesn't carry the Stripe keys, so these run inside the API's own
variables as well: the first `railway run` loads the repo's linked service
(the API), the second adds the database.

```
# Lift a hold by hand, for example an adult the check read as under 18
railway run railway run --service Postgres bash -c 'DATABASE_URL="$DATABASE_PUBLIC_URL" .venv/bin/python scripts/review_safety_event.py --lift-hold HOLD_ID --note "Emailed me: 47, not 17" --commit'

# Close an under-18 account: always a dry run first, then again with --commit
railway run railway run --service Postgres bash -c 'DATABASE_URL="$DATABASE_PUBLIC_URL" .venv/bin/python scripts/review_safety_event.py --close-minor rider@example.com'
railway run railway run --service Postgres bash -c 'DATABASE_URL="$DATABASE_PUBLIC_URL" .venv/bin/python scripts/review_safety_event.py --close-minor rider@example.com --commit'

# Close one when I know from elsewhere they're under 18 and no hold is on record
railway run railway run --service Postgres bash -c 'DATABASE_URL="$DATABASE_PUBLIC_URL" .venv/bin/python scripts/review_safety_event.py --close-minor rider@example.com --force --age 15 --note "Parent emailed: rider is 15" --commit'
```

Check every Stripe dry run against the rider's customer page in Stripe. If
the rider has a Stripe customer and the keys didn't load, the tool stops
with nothing changed and says so; fix that before going on.
[CONFIRM on the first real use that the double `railway run` loads both.]

A note sits inside the single quotes of the command, so it can't contain an
apostrophe: write "did not", not "didn't".

**What each one does:**

- **No flags:** every unreviewed event, newest first: its id, time and the
  rider's email; its kind and where it came from; the words that matched;
  the hold and whether it's open; whether I was emailed ("alerted"); then
  what the rider wrote and the coach's reply. Failed ordinary replies are
  counted but hidden (`--include-failures` shows them). Below that, every open
  hold only Forma can lift, with its id. `--limit N` shows more than 50.
- **`--reviewed EVENT_ID --note "..."`:** stamps `reviewed_at` and the note.
  A second review keeps the first date and adds a dated line to the note.
- **`--lift-hold HOLD_ID --note "..."`:** lifts that hold by hand and marks
  its unreviewed events reviewed with the same note. An under-18 hold lifts
  with every other under-18 hold on the account, and renewal comes back on
  for any subscription Forma stopped (never one the rider cancelled). If
  Stripe can't be reached, it says so and gives the dashboard link to do it
  by hand. If the date of birth on file says under 18, it clears it, so the
  app asks the rider for it again and the adult isn't held again at their
  next re-acceptance. For 180 days after, the check no longer reads an age
  into their words.
- **`--close-minor EMAIL`:** needs an open under-18 hold. It cancels every
  Stripe subscription now, and changes nothing at all if Stripe can't
  cancel. It closes the account through the GDPR path (locked out now,
  purged after the retention window), marks the account's unreviewed events
  reviewed (the note defaults to "Closed as under 18 on review"), then prints
  the refund steps with a link to each payment. The under-18 hold stays on
  the record, so the safety records are kept to the under-18 rule.
- **`--close-minor EMAIL --force`:** for a rider I know is under 18 from
  somewhere other than the chat. It puts an under-18 hold on the record
  first.
- **`--age N`:** with `--close-minor`, the age the rider gave (1 to 17). It
  goes on the record and keeps the safety records through the 21st birthday
  it implies. With no age on the record, they are kept as if the rider were
  13 when flagged: eight years.

## Within 24 hours of an alert

### Crisis

1. **Read it.** Run the list. The event shows as `kind: crisis`, with what
   the rider wrote and the coach's reply word for word.
2. **Check the reply.** The crisis card showed before the coach's text. The
   reply was warm and brief, didn't argue with the feeling or use training
   data, and asked directly whether they're having thoughts of hurting
   themselves or ending their life. It gave Samaritans (116 123), SHOUT (text
   85258), NHS 111's mental health option and their GP, with 999 or A&E for
   immediate danger, or the right numbers for their country (EU 112, US 988
   or 911). It encouraged them to talk to someone they trust today, offered
   to pause the plan (and paused it if they agreed), and promised no contact
   from Forma.
3. **If the reply missed something,** send the rider one email with the
   support numbers (template below) and nothing else. Then add the exchange,
   with names removed, to the safety regression tests and fix the prompt or
   the detector before the next deploy.
4. **If it was a false match** ("I want it all to end so I can taper"), send
   nothing. Add the sentence to the detector's false-match tests before the
   next deploy. The rider still gets no check-in emails or nudges for 14 days
   from the flag; marking it reviewed doesn't end that.
5. **Record it:** `--reviewed EVENT_ID --note "..." --commit`.

### Minor

The rider got fixed words, with no model call: Forma is for adults, so the
coach can't coach them; a British Cycling club or a qualified youth coach,
with a parent or guardian involved; then "This account is on hold and will be
closed, and anything you've paid will be refunded. If you're 18 or over and
this was a mistake, email gareth@ridewithforma.com and I'll sort it out." In
a crisis turn the email line is left out, so the reply ends by pointing to
people. The hold is `hold_all` and only I can lift it. Forma has already set
any subscription to end with the period paid for, and checkout and the
billing portal refuse the account while it's held.

1. **Read it.** Run the list and check the coach's reply is those words. The
   hold id is on the event's `hold:` line.
2. **Decide.**
   - **Plainly a false match** ("I'm 16 and 17 watts up on my last test"):
     `--lift-hold HOLD_ID --note "..." --commit`, then send the lifted
     template.
   - **Unclear:** email once asking them to confirm they're 18 or over
     (template). The hold stays. If they confirm, lift it as above. If there's
     no reply within 14 days, close it as credible.
   - **Credible:** run `--close-minor` as a dry run, check it against Stripe,
     then run it with `--commit`. Refund every payment it lists, following
     the steps it prints. Then send the closing template. The rider's email
     address is at the top of the tool's output; copy it before the commit.
3. **Held again after a lift?** For 180 days after I lift an under-18 hold,
   the check reads no age into the rider's words, so this only happens if
   they give an under-18 date of birth or the coach flags them on purpose.
   If it's still a false match, lift it again and say so in the note.

### A red-flag reply failed

The model failed on a turn with an urgent red flag (chest pain, fainting,
palpitations, a head injury, a fever, a crisis or a minor). The rider got the
fixed safety reply instead: the answer, what to do now, the numbers for their
country, and the hold and how it lifts. The email names the red flags, never
the rider's words.

1. **Read it.** Run the list. The turn shows as `kind: reply_failed` with the
   red flags as `matched`, usually beside the red flag's own event for the
   same message. Check the coach's reply is the fixed one, with the right
   numbers.
2. **If the fixed reply is missing or wrong,** treat it as a bug to fix
   before the next deploy. For a crisis, also send the crisis template.
   Nothing else goes to the rider (rule 1).
3. **Check whether it was only this rider.** An ops alert in the same hour
   means replies are failing for everyone (below).
4. **Record both events,** the failed reply and the red flag's own, with
   `--reviewed`.

### Credit balance empty

Every coach reply fails until the Anthropic account is topped up: chat,
voice, briefings and the coach's emails. A rider with a red flag still gets
the fixed safety reply. Everyone else sees "I couldn't finish a reply just
now. Try sending that again in a moment." It emails on the first failure,
then at most once an hour while it lasts.

1. **Top up** the Anthropic account's credit on the console's billing page,
   and check the auto-reload setting while I'm there.
2. **Confirm replies are back** by sending the coach one message from my own
   account.
3. **Catch what was missed.** Run the list with `--include-failures`. A
   red-flag turn during the gap is a `reply_failed` event with its red flags
   as `matched`: handle it as above. Failed ordinary replies need no review.

### Replies failing for everyone

More than three coach replies failed across all riders inside ten minutes.

1. **Find the cause** on the Anthropic status page, in Sentry and in the
   Railway logs.
2. **Once it's fixed,** do steps 2 and 3 of the credit section.

## After every review

1. **Record the review,** false alarms included. Every alert ends with
   `reviewed_at` and a note of what I checked and what I did.
2. **Delete the alert email** from the inbox and the bin once the review is
   recorded. The record lives in `safety_events`. Never forward an excerpt,
   paste it into another tool or AI chat, or discuss it with anyone who
   doesn't need to know.

## Every morning

Run the list anyway: an alert email can fail. `alerted: no` on an event that
should have emailed me (a crisis, a minor, or a failed red-flag reply) means
the email never came, so I handle it as if it had just arrived. The one
exception is a second event of the same kind for the same rider inside 30
minutes of one that did email. Other kinds (chest pain, fever, a head injury,
an injury, a pregnancy and the rest) never email, so `no` is normal for them.
I read those in the same pass and mark them reviewed, so the list only ever
holds what's new.

If I can't check for more than 24 hours, [CONFIRM: name a cover person; they
need production access and a line in the privacy policy].

## Templates (use the numbers for the rider's country)

**Crisis, only when the coach's reply missed the numbers.** "Hi [name], I'm
Gareth, Forma's founder. I read your conversation with the coach on [date],
and I want to make sure you have these numbers. I'm not a counsellor, so I
won't give advice, but these people are there for exactly this, any time:
Samaritans, free, day or night, on 116 123. Text SHOUT to 85258. In England,
NHS 111 and choose the mental health option. Your GP. If you're in
immediate danger, call 999 or go to A&E. Your plan can wait: reply "pause"
and I'll pause it. Gareth"

**Minor, checking age.** "Hi [name], Forma is for adults, 18 and over.
Something you said to the coach made me want to check: can you confirm
you're 18 or over? Until you do, the coach won't set any training and your
membership won't renew. Gareth"

**Minor, lifted.** "Hi [name], thanks for confirming, and sorry for the
mix-up. I've lifted the hold, so the coach is back and your membership
renews as it did before. Gareth"

**Minor, closing.** "Hi [name], Forma is for adults, so I've closed your
account and refunded everything you paid. It should reach your card within
5 to 10 working days. If you want a coach, a British Cycling club can put
you in touch with a qualified youth coach, with a parent or guardian
involved. Gareth"

The tool's behaviour is pinned by `tests/test_review_tool.py`, and the
commands and alert subjects on this page by
`tests/test_safeguarding_protocol_doc.py`.
