# Launch audit checklist

Written 4 October 2026 for the doors-open week. Two sources: the PRD (launch
gates in `launch-plan.md`, Appendix B and Appendix I of `cycling-coach.md`)
and what any paid UK subscription product owes its customers on day one.
Each line is checked against the live system, not against the code's intent.

Severity: **MUST** blocks the doors. **SHOULD** is fixed this week if it can
be. **LATER** is logged and left.

## A. Truth: every claim is true today

- [ ] A1 MUST. Every promise on the landing page is something the product does today (outreach, memory, Wahoo, plan rewrites, price, founding mechanics).
- [ ] A2 MUST. Price and founding terms match Stripe exactly (£14.99 locked for as long as they stay; £19.99 list; a hundred places).
- [ ] A3 MUST. No dates that have passed or that nobody can keep (15 September, "this week", "soon" with no owner).
- [ ] A4 MUST. Emails, the coach's product knowledge and the app describe the same product (no live Strava, invite-only, how billing works).
- [ ] A5 SHOULD. Numbers in the journal are sourced (testing data, PBs, watts) and consistent across posts.

## B. Voice: it reads like Gareth, not like a model

- [ ] B1 MUST. Copywriter law: no em or en dashes, British English, no "kicker", no widows or orphans on the landing page.
- [ ] B2 MUST. No AI tells (the twelve in `ai-tells.md`): no "not X, it's Y" reflexes, no triplets for rhythm, no mannered prose, no hedged filler.
- [ ] B3 SHOULD. First person where Gareth speaks, the coach's voice where Forma speaks, and the two never blur in one surface.
- [ ] B4 SHOULD. Jargon is glossed at first use on every surface a new rider sees (TSS, FTP, IF, CTL, ERG).

## C. Conversion: the landing page does one job

- [ ] C1 MUST. The waitlist form works from a fresh browser, on a phone, and Letter 0 arrives (SPF/DKIM pass).
- [ ] C2 MUST. Every CTA goes somewhere that works today (rider login is invite-only; does it say so?).
- [ ] C3 SHOULD. The page answers the five buying objections before the form: what is it, is it for me, what does it cost, why trust you, what happens next.
- [ ] C4 SHOULD. Open Graph card, favicon, title and description are right when the link is pasted into WhatsApp, iMessage, LinkedIn.
- [ ] C5 SHOULD. Mobile: no horizontal scroll, tap targets at least 44px, hero image loads fast (LCP under 2.5s on 4G).

## D. Storytelling: the journal earns trust

- [ ] D1 SHOULD. Each post opens on a scene or a number, not a thesis.
- [ ] D2 SHOULD. Each post ends on something the rider can use on this week's rides.
- [ ] D3 SHOULD. Each post links back to the list without a hard sell.

## E. Coaching: the coach a new rider meets

- [ ] E1 MUST. A brand-new rider with no rides, no goal and no plan gets a useful first reply, not one built on assumptions.
- [ ] E2 MUST. The coach asks before it assumes (location, availability, equipment) and never invents data.
- [ ] E3 SHOULD. Decisions are made for the rider ("do this today") with the reason in one line.
- [ ] E4 SHOULD. Medical and safety boundaries hold (injury, chest pain, eating disorders).
- [ ] E5 MUST. The safety red team is re-run against the live model and no reply is rated unsafe (launch gate 5f). The same run checks the safety classifier: the round 4 sweep (303 ordinary and 84 red-flag messages: ROUND4_ORDINARY and ROUND4_MISSED in tests/test_safety_screen.py) through the live classifier, and its latency in forma_calls (task safety_classify) mostly under its 1.5 s limit.
- [ ] E6 MUST. Ride mode on a real Kickr: ERG never above the cap, sprints above it run with ERG released, Pause eases the trainer and Stop releases it at once (gate 5f).

## F. New-rider journey, end to end, as someone who is not Gareth

- [ ] F1 MUST. Invite code: valid code registers; wrong, used-up or expired code is refused with a human message.
- [ ] F2 MUST. Verification email sends, the link works, an unverified rider is handled sensibly.
- [ ] F3 MUST. Login, refresh and logout; password reset email and link work.
- [ ] F4 MUST. Onboarding completes; the rider lands on a dashboard that tells them the next step.
- [ ] F5 MUST. Goal creation and plan generation work for a rider with no history.
- [ ] F6 MUST. Ride data in: FIT/GPX upload works; Wahoo connect works for a second Wahoo account; archive import path works.
- [ ] F7 MUST. Paywall: with `REQUIRE_SUBSCRIPTION=true`, an unpaid rider is sent to checkout with a clear reason, and a paid rider is never blocked.
- [ ] F8 MUST. Checkout to active to portal to cancel to access ends at period end. Webhooks are the only source of truth.
- [ ] F9 SHOULD. Activation card and outreach engine pick up a new rider at the right stage.
- [ ] F10 SHOULD. Account export and deletion work from Settings.

## G. Integrations and platform rules

- [ ] G1 MUST. Wahoo app is cleared for production use by riders other than the developer.
- [ ] G2 MUST. Strava API data is not offered to new riders (API Agreement s5.3 bans AI use); the archive import is the Strava door.
- [ ] G3 SHOULD. Strava and Wahoo consent screens say FORMA, not "Cycling Coach".

## H. Billing and tax

- [ ] H1 MUST. Live mode: account verified, live £14.99 price, live webhook, live portal settings, live keys on Railway.
- [ ] H2 MUST. Sandbox customer IDs cleared from real rider rows before live keys go in.
- [ ] H3 MUST. Stripe Tax on, with address capture at Checkout, before the first EU sale (OSS registration).
- [ ] H4 SHOULD. Receipts and failed-payment emails switched on in Stripe; statement descriptor reads RIDEWITHFORMA.COM.

## I. Legal: what UK law requires on day one

- [ ] I1 MUST. Privacy policy live and linked from the landing page, register page and app footer (lawful basis, health data, the founder reading conversations, coach emails, processors, retention, rights, contact).
- [ ] I2 MUST. Terms of service live (subscription, price lock, cancellation, the 14-day cooling-off right for digital services and its waiver at checkout, "not medical advice", liability).
- [ ] I3 MUST. Explicit consent for health data (special category under UK GDPR Article 9) captured at registration or onboarding.
- [ ] I4 MUST. ICO registration paid.
- [ ] I5 MUST. Company disclosures on the website: FORMA CYCLING LTD, company number 17479029, registered office, registered in England and Wales (Companies (Trading Disclosures) Regulations 2008).
- [ ] I6 SHOULD. Cookie position stated (no tracking cookies means no banner, but say so in the privacy policy).
- [ ] I7 SHOULD. Liability insurance (professional indemnity for coaching advice).

## J. Operations

- [ ] J1 MUST. Production deploys are reproducible: dependencies locked so a library release cannot break a build (the SQLAlchemy 2.1 lesson).
- [ ] J2 MUST. A failed migration stops the deploy instead of starting the app on the old schema.
- [ ] J3 MUST. Database backups exist and one restore has been tried.
- [ ] J4 MUST. Sentry catches backend and frontend errors; uptime monitor on the API and the app.
- [ ] J5 SHOULD. Deleted-account purge is scheduled.
- [ ] J6 SHOULD. Per-rider cost cap holds and alerts.
- [ ] J7 SHOULD. Vercel deploy step is in the release ritual, or automated.
- [ ] J8 MUST. The safeguarding protocol runs from day one: the review list works against production, and one dry run of the Stripe form of the tool matches the rider's page in Stripe.

## K. Security

- [ ] K1 MUST. One rider cannot read another rider's data (isolation tests pass against the current code).
- [ ] K2 MUST. No secrets in git; admin endpoints locked to `ADMIN_EMAILS`.
- [ ] K3 SHOULD. Rate limits on auth, waitlist and coach endpoints.
- [ ] K4 MUST. FORMA_EDGE_SECRET is set on Vercel and Railway, so a direct call to Railway with a forged x-vercel-id can't choose its own address (gate 5f).
- [ ] K5 MUST. ip-check: https://app.ridewithforma.com/api/v1/auth/ip-check, opened in a browser, shows your own address with via_edge true, and a direct call to Railway shows via_edge false (gate 5f). Only after K4: until the secret is set on Railway, ip-check always answers client_ip null and via_edge null. That is on purpose (it would otherwise show a forger that forging works), not a fault.

## L. Everything works on a phone

- [ ] L1 MUST. Register, onboarding, dashboard and coach usable at 375px.
- [ ] L2 SHOULD. Add to Home Screen gives a proper icon and name (PWA manifest).

---

## Results, 4 October 2026

Method: a fresh rider registered against production with a single-use code
(AUDITOCT4, now revoked), went through verification, onboarding, goal, plan,
ride upload, the coach, Wahoo and Strava links and Stripe checkout; three
parallel audits covered copy, in-app copy and the code. Everything marked
FIXED is deployed (API 6eba62f onwards, app on Vercel, landing 9923c1c) and
re-checked on production unless it says otherwise.

| Item | Result |
| --- | --- |
| A1-A4 truth | FIXED. Passed 15 September date, false "doors close", "unsubscribe in a click", "no store app", data FAQ (Anthropic named), billing truth in the coach's knowledge |
| A5 journal numbers | OPEN. Unsourced figures listed in the report for Gareth to confirm |
| B1-B4 voice | FIXED across landing, journal, app, emails, coach prompts. Coach still writes the odd comma splice; rule tightened |
| C1 waitlist | PASS. Mobile fields were 140-190px tall, FIXED |
| C2-C5 | FIXED (invite-only said plainly, OG card re-rendered, orphans) |
| D1-D3 journal | PASS |
| E1-E2 coach first contact | FIXED. Blind to the week on a Sunday; now sees seven days ahead with watts and the app's session names |
| F1 invite codes | PASS |
| F2 verification | PASS |
| F3 login | FIXED. Email case locked riders out; a wrong password reloaded the page silently |
| F4 onboarding | PASS |
| F5 plan | FIXED. No long ride for any event, 6-week tapers, tapers with no goal |
| F6 data in | FIXED. Duplicate uploads double-counted; uploads failed after 30 minutes idle |
| F7 paywall | FIXED. Unpaid riders now see a membership banner and the real reason |
| F8 billing | FIXED. Double checkout, out-of-order webhooks, no confirmation on return |
| F9 activation | FIXED. Four of five next steps pointed somewhere wrong |
| F10 export/delete | FIXED. Deletion now cancels the membership and cuts Wahoo |
| G1 Wahoo production | OPEN. Gareth: check Sandbox or Production in the Wahoo portal |
| G2 Strava | FIXED. Reconnect only; button removed |
| G3 consent screens | OPEN. Rename Strava app; Wahoo app name |
| H1-H4 Stripe live | OPEN. Gareth mid-activation |
| I1-I2 privacy, terms | DRAFTED in prd/legal/. Gareth reviews; then publish |
| I3 health consent | FIXED. Required tick box at registration, timestamped |
| I4 ICO | OPEN. Gareth |
| I5 company disclosure | FIXED on every landing and journal page |
| J1 lock | FIXED. constraints.txt, Python 3.13 image |
| J2 migrations | FIXED. Failed migration fails the deploy |
| J3 backups | OPEN. Gareth: confirm Railway Postgres backups and try one restore |
| J4 monitoring | PASS. Point UptimeRobot at /health/deep |
| J5 purge | FIXED. Reads every table from the database, runs daily; first run removed 7 old test accounts |
| J7 Vercel | OPEN. Still manual |
| K1 isolation | PASS (4 tests, after local DB migrated) |
| K2 secrets | PASS. WAHOO_WEBHOOK_TOKEN is NOT set: OPEN, Gareth |
| K3 rate limits | FIXED. Keyed on the proxy-seen address; voice behind paywall and limited |
| L1 phone | PASS on landing; app checked at 375px in earlier sessions |

## Update, 8 October 2026

The safety system was built and re-verified on 8 October with the model
mocked. What still has to be proved on the live system is launch gate 5f,
tracked here as E5, E6, K4 and K5.

| Item | Result |
| --- | --- |
| E4 safety boundaries | BUILT. Red-flag check, holds, fixed replies and founder alerts, with tests. Live-model proof is E5 |
| E5 red team on the live model | OPEN. Gareth |
| E6 Kickr test | OPEN. Gareth, on his own Kickr |
| J8 safeguarding routine | OPEN. Review tool written; the Stripe form of the command needs one confirmed dry run |
| K3 rate limits | REOPENED until K4 and K5 pass. A forged x-vercel-id got round every address limit, and the keying has not been checked on a real request |
| K4 edge secret | OPEN. Gareth sets FORMA_EDGE_SECRET on Vercel (and redeploys the frontend), then on Railway |
| K5 ip-check | OPEN. Gareth opens the ip-check through the app after K4. Before K4 it shows nulls by design |
