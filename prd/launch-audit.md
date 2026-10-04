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

## K. Security

- [ ] K1 MUST. One rider cannot read another rider's data (isolation tests pass against the current code).
- [ ] K2 MUST. No secrets in git; admin endpoints locked to `ADMIN_EMAILS`.
- [ ] K3 SHOULD. Rate limits on auth, waitlist and coach endpoints.

## L. Everything works on a phone

- [ ] L1 MUST. Register, onboarding, dashboard and coach usable at 375px.
- [ ] L2 SHOULD. Add to Home Screen gives a proper icon and name (PWA manifest).
