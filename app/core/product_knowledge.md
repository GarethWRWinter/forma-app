# Forma product knowledge

What Forma does today and where each control lives. Relay instructions using the on-screen labels exactly as written here.

## Getting set up

Forma is invite-only while the founding hundred fills; at launch there is one shared invite code.

To join: ridewithforma.com, then Get started, then Invite code, Full name (optional), Email, Password (at least 8 characters), then Create account. A valid invite during the invite-only period assigns a founding number, 1 to 100, never reissued.

Onboarding follows: What are we aiming at?, Tell me about race day (event riders only), How much bike is in your life? (hours, years, Which days can hurt?), The engine, roughly, Meet Forma (tone, optional coach name), then Start training, which writes the first plan. If that fails, the screen offers Write the plan again, then Go to my dashboard; build later from Goal, then Build my season.

A verification email, "One click and your coach is ready", follows; the link lasts 24 hours. Unverified riders are not locked out; a banner offers Resend the link.

Navigation. The sidebar (menu icon on a phone): Today, Coach, Rides, Form, Goal, Brain, Palmarès, Settings. Form opens the page headed Performance; Goal opens the training plan. The Goals list has no sidebar entry: Goal, then All goals, or Settings, then Goals, then Full goals page.

Numbers: Settings, then Profile (Full name, Weight (kg), FTP (watts), Max HR (bpm), Resting HR (bpm), Hours a week for the bike, Which days can hurt?), then Save profile. A 20-minute test: Settings, then FTP test, then 20-minute average power (w), then Calculate; FTP becomes 95 percent of it.

Log in remembers a rider for 30 days by default.

## Data in

Settings, then Data in holds four cards: Wahoo, Ride archive, Strava, Dropbox.

### Wahoo

Connect: Settings, then Data in, then Connect Wahoo. The rider signs in at Wahoo and returns to "Wahoo linked. Any rides from while it was disconnected are on their way." The first link imports history in the background; after that each ride arrives when the ELEMNT syncs.

Import full history shows "Reading your history N / total". Imported rides are skipped, so it is safe to run again; no per-ride debriefs. If it stops: "Import stopped. Not your fault. Retry and it picks up where it left off." with Retry import.

Fetch missing rides checks the last 30 workouts at Wahoo. Disconnect revokes Forma's key at Wahoo first, then removes it locally.

Needs reconnecting. When Wahoo stops accepting Forma's key, the card shows Needs reconnecting and one email arrives, "Wahoo has stopped talking to Forma". Rides sent while the link is broken are dropped by Wahoo, not queued; Forma runs a catch-up sync on reconnect. Two cases:

- Ordinary: Settings, then Data in, then Reconnect Wahoo on the Wahoo card.
- The ten keys cap: Wahoo lets one app hold at most ten live keys per rider. At the cap, Reconnect Wahoo fails with "Wahoo refused the reconnect". The rider must first deauthorise Forma in the Wahoo app: Settings, then Authorized Apps, then Forma, then Deauthorize (or at wahooligan.com/profile), then use Reconnect Wahoo.

### File upload

Rides, then Upload a ride file, top right. Accepted: .fit, .gpx, .tcx and .gz versions, up to 30MB (the on-screen message says 50MB; 30MB is the real limit). Upload needs an active membership when the gate is on.

### Strava archive

Settings, then Data in, then Ride archive. The rider requests the archive at Strava (Settings, My Account, Download Request), then uses Choose the zip. The zip is read in the browser; only the ride files inside are sent. The card shows "N rides found", then How far back: Everything, Last 3 years, Last 12 months. Finished: "History in. N rides imported, N already on record, N unreadable." Duplicates of existing rides are skipped. No per-ride debriefs; fitness is rebuilt once at the end. Garmin archives are a zip of zips: unzip once, then choose one inner zip. Live Strava linking is not part of Forma: Strava's API terms do not allow its data to be used by an AI coach. If a rider asks to connect Strava, point them to the archive import above; their complete history comes across that way.

### Dropbox for Garmin

Garmin riders import their archive, then use Dropbox for new rides: Settings, then Data in, then Connect Dropbox. Once linked the card shows Sync folder (default /cycling) with Change, Sync FIT files and Disconnect. Forma checks the folder every 15 minutes and imports new .fit files only; .gpx and .tcx there are ignored. A bridge such as tapiriik can copy each Garmin ride into the folder; Forma does not supply it.

## Rides

Rides lists every ride with date, source badge (Strava, Upload, In-app, Dropbox), dominant zone, title, one-line story, NP, TSS and IF. Tap a row to open it.

On import Forma computes Normalised Power, Intensity Factor, Variability Index and TSS. Zones are Coggan Z1 to Z7 as percent of FTP. Rides without power use heart-rate zones and an estimated TSS. The first GPS fix names the location; start-line weather is attached when the ride is imported within 14 days.

Forma titles each ride (2 to 6 words) with a one-sentence story. Rename: open the ride, tap the pencil icon (Rename this ride), Save; a custom title is never overwritten. GPX on the ride page downloads the file. The Post-ride debrief is written once per ride for uploads, Wahoo, Dropbox and Ride mode recordings; history and archive imports do not get one.

## Goals

A goal has a name, date, type (Road Race, Criterium, Time Trial, Gran Fondo, Sportive, Gravel, MTB, Hill Climb, Stage Race, Charity Ride, Century), priority (A race, B race, C race), optional target time in minutes, route URL, notes and GPX file, plus a why and a becoming written with the coach. You can create or update a goal in chat when the rider asks.

In the app: Goal, then All goals, then Craft it with Forma (chat) or Know the details already? Quick add (form), then Create goal. That builds a new plan automatically. Adding at Settings, then Goals, then + Add goal does not.

The goal detail page (tap the name) holds Edit, Delete, Why this one, The coach's read (Get the read), Course profile (Upload GPX file), Fitness readiness (On Track, Needs Work, At Risk) and, given FTP, weight and a GPX, The projection.

After the date the goal shows Race report pending with File the report: The result (Completed, DNF or DNS, time, position), The ride file, Gut check, Under the skin, For the record, then File the race report. Debrief then opens chat with the result typed.

## The plan

Build or rebuild: Goal, then + Build my season, then Periodisation model (Traditional, Polarized, Sweet Spot), then Generate. Building cancels the current plan. It runs from today to the A race (12 weeks with no goal, never under 4) through base, build, peak and a race week. Every third week (beginner) or fourth (otherwise) is a recovery week at 60 percent. Rest days are never scheduled; hard days carry intensity.

Change hard and rest days: Goal, then Adjust availability, tap each day (Easy, Rest, Hard), Save schedule, then rebuild.

Each day card's session has a tick (Mark completed) and a cross (Skip). Tap the title for the workout page: Start Session opens Ride mode; Export offers .ZWO Zwift, .ERG Wahoo / TrainerRoad, .MRC % FTP format, .FIT Garmin / Hammerhead. Exports need an FTP. Imported rides match that day's session automatically.

You may edit the current week in chat (change, move, add, skip) once the rider confirms. Larger changes go through a proposal card on Today and Goal: Make the change, Talk it through, Not now. The rider can use Goal, then Ask [coach] to review this plan; the answer is a proposal or "The plan still stands." A review needs at least 3 recent rides unless a goal changed.

## Briefings and Race Radio

Pre-ride briefing on Today is written once a day: today's session, conditions, clothing, chain prep, one question. Weather is read at the start of the most recent GPS ride; with none, the briefing says it has no forecast. On a goal day the card becomes Race day · the team car: a longer briefing with pacing in watts and, given a GPX, headwind, tailwind and crosswind by kilometre.

Race Radio lives in Ride mode: Today, then Start ride, or the workout page, then Start Session. It needs an FTP or refuses. Connect your devices lists Power Meter, Heart Rate Monitor, Smart Trainer (ERG) and Cadence Sensor over Bluetooth in Chrome, Edge or Brave on Mac, PC or Android; iPhone and iPad are not supported. Race Radio lines are pre-written templates chosen by the step, spoken in the coach voice; Radio mutes them. Stop offers Save ride & end, Discard & end, Keep going. A saved session becomes a ride and completes the workout.

## The coach

Coach opens chat. + New Chat starts a thread; Coach resumes the latest. Chat options per thread: Rename, Pin, Star, Archive, Delete. Delete removes the conversation for good; what Forma learned stays. Enter sends, Shift+Enter adds a line.

Name and tone: Settings, then Your coach. Tones: Balanced, Empathetic & nurturing, Stoic & calm, Direct & no-nonsense, Analytical & data-deep, Playful & witty. Prefer another name? renames the coach (30 characters); Save coach. There is no coach picture.

Memory. After chats, voice chats, debriefs and onboarding, Forma files memories. Brain shows them as a graph with a reading. To hide one: click it, then Hide. Hidden memories still guide judgement but are never raised. Hiding is not deleting; memories go only with the account.

Attachments. The paperclip (Attach a ride file (GPX, FIT or TCX)) takes up to 3 files a message, 25MB each, gzipped or not. An attached file is analysed but not filed: nothing enters Rides or fitness unless the rider agrees to save it, and a saved file is never saved twice. Ask once whether it is the rider's own ride, whether to save it, or whether it is someone else's ride or a route.

Voice. The mic shows in browsers with speech recognition (Chrome, Edge, Safari 14.1 or later; not Firefox). Tap it and speak. Replies are read aloud; the speaker button mutes the voice; tapping the mic mid-reply interrupts.

Each rider has a monthly conversation budget. When spent, chat replies with the quota message: the quota resets on the first; the plan, rides and briefings keep working; everything the rider says still goes into memory. Plan review and initiatives pause; nudges and debriefs fall back to plain text.

## Membership and money

Forma costs £19.99 a month. The founding hundred pay £14.99 a month, fixed for as long as they stay. Forma is in its founding preview: billing has not opened yet and nobody is charged during the preview. The founding hundred will be invited by letter, with one shared invite code, when billing opens. Do not promise a date; say the founder will write to them when the doors open. Founding riders see "Founding rider · n of 100" on Palmarès with Download your badge.

Settings, then Membership shows Active, Trial, Payment issue, Cancelled or Not a member yet. Join Forma (Stripe checkout) and Manage billing (cards, invoices, cancellation) appear only once billing is switched on; during the preview they are not there and nothing needs paying. A failed payment shows "Your last payment didn't go through. Update the card and nothing is interrupted, Stripe retries for a few days." Access continues during retries. With the gate on and no active membership, uploads, chat, attachments and Ride mode recording answer: "Your Forma membership isn't active. Join from Settings and the coach is yours again."

## Your data

Export: Settings, then Data out, then Download my data. The JSON file holds account details, onboarding answers, goals, ride summaries, plans, workouts, daily fitness numbers, chats, nudges and memories. It excludes per-second ride data, original FIT files and connection keys.

Delete: Settings, then Data out, then Delete my account, type the account email, then Delete it. The rider is logged out at once, the account closes, and Strava and Dropbox connections are removed immediately. Remaining data is purged after 30 days. It cannot be undone; take the download first.

## When something breaks

- Cannot log in ("That email and password don't match."): Forgotten password?. The link lasts one hour; check spam. A reset signs out every device.
- A message did not send ("That one didn't reach me", "Sorry, I had trouble connecting"): send it again.
- A Wahoo ride is missing: Settings, then Data in, then Fetch missing rides. If the card says Needs reconnecting, follow the Wahoo section.
- An import stopped: Retry import or Try again. Skipped rides are never duplicated.
- "No ride files in this zip": no .fit, .gpx or .tcx inside, or a Garmin zip of zips (unzip once first).
- Ride mode or Export refuses: Settings, then Profile, then FTP (watts).
- "I can't reach your numbers right now": wait a minute and refresh. "Something slipped a gear.": Try again. "This road does not exist.": Back to the dashboard.
- Membership message: Settings, then Membership, then Join Forma or Manage billing.
- If the answer is not in this document, say so plainly and give gareth@ridewithforma.com.

## Not yet documented

- Whether riders are warned before the monthly conversation budget runs out.
- Whether the Wahoo connection is revoked automatically on account deletion, and when the 30 day purge runs.
- What the briefing says before any GPS ride exists.
- TrainingPeaks: nothing rider-facing exists yet.
- The exact wording of the verification and reset emails.
- After renaming the coach, a few labels still say Forma.

If a rider asks about any of these, say plainly that you are not sure and give gareth@ridewithforma.com.
