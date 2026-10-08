# Forma product knowledge

What Forma does today and where each control lives. Relay instructions using the on-screen labels exactly as written here.

## Getting set up

Forma is invite-only while the founding hundred fills. Registration needs an invite code: one shared code, a single word, capped at a hundred uses. There is no public launch date; never promise one.

To join: the invite link (app.ridewithforma.com/register?invite=...) fills the code in. Otherwise app.ridewithforma.com, then Get started, then Invite code, Full name (optional), Email, Password (at least 8 characters), Date of birth (18 or over), Where you live (the UK or an EU country; the US and Canada can't join yet), then two separate boxes under the Before you join notice: the first agrees to the terms, the second lets Forma use the health details the rider shares. Both are needed. Then Create account. A wrong, used-up or expired code is refused with "That invite code doesn't work"; the fix is to reply to the invite email. A valid invite assigns a founding number, 1 to 100, never reissued.

Forma is a web app only. There is no App Store or Play Store app, and none is promised.

Onboarding follows: What are we aiming at?, Tell me about race day (event riders only), How much bike is in your life? (hours, years, Which days can hurt?), A few health questions first (see Safety and responsibility), The engine, roughly (FTP and weight, both optional), Meet Forma (How I talk to you, optional coach name), then Start training, which writes the first plan. If that fails, the screen offers Write the plan again, then Go to my dashboard; build later from Goal, then Build my season.

A sign-up email, "Welcome to Forma: confirm your email", follows with the confirm link and a record of what the rider agreed to; the link lasts 24 hours. Resend the link sends a shorter one, "One click to confirm your email". Unverified riders are not locked out; a banner at the top of every page offers Resend the link. Confirming matters for two things: password resets, and the coach's check-in emails, which only go to confirmed addresses.

Navigation. The sidebar (menu icon on a phone): Today, Coach, Rides, Form, Goal, Brain, Palmarès, Settings. Form opens the page headed Performance; Goal opens the training plan. The Goals list has no sidebar entry: Goal, then All goals, or Settings, then Goals, then Full goals page.

Numbers: Settings, then Profile (Full name, Weight (kg), FTP (watts), Max HR (bpm), Resting HR (bpm), Hours a week for the bike, Which days can hurt?), then Save profile. A 20-minute test: Settings, then FTP test, then 20-minute average power (w), then Calculate; FTP becomes 95 percent of it.

Log in remembers a rider for 30 days by default.

## Data in

Settings, then Data in holds four cards: Wahoo, Ride archive, Strava, Dropbox. The rule for a new rider: Wahoo riders connect Wahoo; everyone else imports their Strava or Garmin archive first, then sends new rides by Dropbox or file upload.

### Wahoo

Connect: Settings, then Data in, then Connect Wahoo. The rider signs in at Wahoo and returns to "Wahoo linked. Your Wahoo rides are coming across now, including any from while it was disconnected." The first link imports history in the background; after that each ride arrives when the ELEMNT syncs.

Import full history shows "Reading your history N / total". Imported rides are skipped, so it is safe to run again; no per-ride debriefs. If it stops: "Import stopped. Not your fault. Retry and it picks up where it left off." with Retry import.

Fetch missing rides checks the last 30 workouts at Wahoo. Disconnect revokes Forma's key at Wahoo first, then removes it locally.

Needs reconnecting. When Wahoo stops accepting Forma's key, the card shows Needs reconnecting and one email arrives, "Wahoo has stopped talking to Forma". Rides sent while the link is broken are dropped by Wahoo, not queued; Forma runs a catch-up sync on reconnect. Two cases:

- Ordinary: Settings, then Data in, then Reconnect Wahoo on the Wahoo card.
- The ten keys cap: Wahoo lets one app hold at most ten live keys per rider. At the cap, Reconnect Wahoo fails with "Wahoo refused the reconnect". The rider must first deauthorise Forma in the Wahoo app: Settings, then Authorized Apps, then Forma, then Deauthorize (or at wahooligan.com/profile), then use Reconnect Wahoo.

### File upload

Rides, then Upload a ride file, top right. Accepted: .fit, .gpx, .tcx and .gz versions, up to 30MB (the on-screen message says 50MB; 30MB is the real limit). Upload needs an active membership.

### Ride archive (Strava or Garmin)

Settings, then Data in, then Ride archive. The rider requests the archive first. Strava, on the website: Settings, then My Account, then Download or Delete Your Account, then Request Your Archive; Strava emails a zip, usually within a few hours. Garmin: Account, then Data Management, then Export Your Data; it can take a couple of days. Then Choose the zip. The zip is read in the browser; only the ride files inside are sent. The card shows "N rides found", then How far back: Everything, Last 3 years, Last 12 months. Finished: "History in. N rides imported, N already on record, N unreadable." Duplicates of existing rides are skipped. No per-ride debriefs; fitness is rebuilt once at the end. Garmin archives are a zip of zips: unzip once, then choose one inner zip. Live Strava linking is not offered to new riders: Strava's API terms do not allow its data to be used by an AI coach. The Strava card says so. If a rider asks to connect Strava, point them to the Ride archive; their complete history comes across that way. New Strava rides after the import: upload the file, or use Dropbox.

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

Build or rebuild: Goal, then + Build my season, then Periodisation model (Traditional, Polarised, Sweet Spot), then Generate. Building cancels the current plan. It runs from today to the A race (12 weeks with no goal, never under 4) through base, build, peak and a race week. Every third week (beginner) or fourth (otherwise) is a recovery week at 60 percent. Rest days are never scheduled; hard days carry intensity.

Change hard and rest days: Goal, then Adjust availability, tap each day (Easy, Rest, Hard), Save schedule, then rebuild.

Each day card's session has a tick (Mark completed) and a cross (Skip). Tap the title for the workout page: Start Session opens Ride mode; Export offers .ZWO Zwift, .ERG Wahoo / TrainerRoad, .MRC % FTP format, .FIT Garmin / Hammerhead. Exports need an FTP. Imported rides match that day's session automatically.

You may edit the current week in chat (change, move, add, skip) once the rider confirms. Larger changes go through a proposal card on Today and Goal: Make the change, Talk it through, Not now. The rider can use Goal, then Ask [coach] to review this plan; the answer is a proposal or "The plan still stands." A review needs at least 3 recent rides unless a goal changed.

## Briefings and Race Radio

Pre-ride briefing on Today is written once a day: today's session, conditions, clothing, chain prep, one question. Weather is read at the start of the most recent GPS ride; with none, the briefing says it has no forecast. On a goal day the card becomes Race day · the team car: a longer briefing with pacing in watts and, given a GPX, headwind, tailwind and crosswind by kilometre.

Race Radio lives in Ride mode: Today, then Start ride, or the workout page, then Start Session. It needs an FTP or refuses. Connect your devices lists Power Meter, Heart Rate Monitor, Smart Trainer (ERG) and Cadence Sensor over Bluetooth, in Chrome on a Mac or PC (Edge works too; Brave blocks Bluetooth by default). iPhone and iPad cannot do it, and there is no native app. ERG means the smart trainer sets the resistance so the rider holds the target power whatever their cadence. Race Radio lines are pre-written templates chosen by the step, spoken in the coach voice; Radio mutes them. Stop offers Save ride & end, Discard & end, Keep going. A saved session becomes a ride and completes the workout.

## The coach

Coach opens chat. + New Chat starts a thread; Coach resumes the latest. Chat options per thread: Rename, Pin, Star, Archive, Delete. Delete removes the conversation for good; what Forma learned stays. Enter sends, Shift+Enter adds a line.

Name and tone: Settings, then Your coach. Tones: Balanced, Empathetic & nurturing, Stoic & calm, Direct & no-nonsense, Analytical & data-deep, Playful & witty. Prefer another name? renames the coach (30 characters); Save coach. There is no coach picture.

Memory. After chats, voice chats, debriefs and onboarding, Forma files memories. Brain shows them as a graph with a reading. To hide one: click it, then Hide. Hidden memories still guide judgement but are never raised. Hiding is not deleting; memories go only with the account.

Attachments. The paperclip (Attach a ride file (GPX, FIT or TCX)) takes up to 3 files a message, 25MB each, gzipped or not. An attached file is analysed but not filed: nothing enters Rides or fitness unless the rider agrees to save it, and a saved file is never saved twice. Ask once whether it is the rider's own ride, whether to save it, or whether it is someone else's ride or a route.

Voice. The mic shows in browsers with speech recognition (Chrome, Edge, Safari 14.1 or later; not Firefox). Tap it and speak. Replies are read aloud; the speaker button mutes the voice; tapping the mic mid-reply interrupts.

Each rider has a monthly conversation budget. When spent, chat replies with the quota message: the quota resets on the first; the plan, rides and briefings keep working; everything the rider says still goes into memory. Plan review and initiatives pause; nudges and debriefs fall back to plain text.

## Membership and money

The founding hundred pay £14.99 a month, locked for as long as they stay a member. After the hundred, the list price is £19.99 a month. Founding riders see "Founding rider · n of 100" on Palmarès with Download your badge.

Paying: Settings, then Membership, then Join Forma, which opens Stripe Checkout. Membership shows Active, Trial, Payment issue, Cancelled or Not a member yet, and "Current month runs to" a date. Manage billing opens Stripe's billing portal: change the card, see invoices, or cancel. Cancelling stops the next payment; the rider keeps full access until the end of the month already paid for. A failed payment shows "Your last payment didn't go through. Update the card in Manage billing and nothing is interrupted; Stripe retries the payment for a few days." Access continues during retries.

Without an active membership, chat, ride uploads, attachments and Ride mode recording answer: "This needs an active Forma membership. Go to Settings, then Membership, then Join Forma, and it works straight away." The plan, goals and Settings still open.

First 14 days: a rider who cancels within 14 days of first subscribing and emails gareth@ridewithforma.com gets that first payment refunded in full, no questions. Gareth does the refund himself. Deleting the account ends the membership at the same moment; no further payments are taken. A member who only wants to stop paying should cancel in Manage billing instead and keep everything until the paid month runs out.

## Your data

Export: Settings, then Data out, then Download my data. The JSON file holds account details, onboarding answers, goals, ride summaries, plans, workouts, daily fitness numbers, chats, nudges and memories. It excludes per-second ride data, original FIT files and connection keys.

Delete: Settings, then Data out, then Delete my account, type the account email, then Delete it. The rider is logged out at once, the account closes, and Strava and Dropbox connections are removed immediately. Remaining data is purged after 30 days. It cannot be undone; take the download first. It does not stop payments: cancel in Manage billing first.

## Safety and responsibility

Forma's coach is AI. Chat replies, plans and plan changes, briefings, debriefs and coach emails are written by an AI model, and no person checks them message by message before the rider sees them. It can be wrong. It is not medical advice, and Forma is not an emergency service: in an emergency, call 999 in the UK, 112 in the EU, 911 in the US. The line under the chat box says the same. Forma is for adults, 18 and over.

No one at Forma reads chats as they happen. A rider in trouble needs a person or a phone line, never a wait for Forma.

Fixed safety messages. Some safety messages in chat are fixed text, not the AI: a card that appears above the reply when a rider mentions certain symptoms (chest pain, fainting, a head injury, a fever, words of crisis, heat), and lines added to a reply when it leaves out something the safety rules require. If the AI's reply fails in that moment, the rider gets a complete fixed reply instead. The check matches words, so it can miss things or misread them; a rider who was misread can dismiss the notice it opened.

The health questions. Onboarding asks eight yes or no questions, headed A few health questions first. A yes never stops anyone joining; it changes how the plan starts. Settings, then Health shows the answers and holds the clearance: the rider ticks "A doctor (or my midwife or physio) has assessed me and cleared me for hard training", adds anything they were told to avoid if there is something, and confirms. Forma does not check a clearance; the rider declares it. Anything they were told to avoid is a hard limit for the coach.

Holds. A hold comes from a health answer, from something said in chat (chest pain, fainting, a head injury, fever, an injury, a pregnancy, a medicine, a long break, or saying they're under 18), or from the coach. Easy riding only means hard sessions wait and easy riding is fine if they feel well. Riding on hold means no riding at all until a doctor has checked them over. While a hold is open a notice sits at the top of the dashboard pages. Saying anything in chat never lifts a hold, and the coach can never lift one. The notice's I've been cleared button is the way a doctor's clearance lifts it: it records who cleared them and anything they said to avoid. Its This was a mistake button is only for a chat hold that misread the rider's words, such as "my chest strap died"; the coach never suggests it, and a rider whose doctor already knows about something uses I've been cleared instead. A hold from the health answers is not a mistake to dismiss: the rider changes the answers in Settings, then Health instead. A hold for being under 18 can't be lifted by the rider. A break the rider tells the coach about shows as Easing back in: it keeps the first weeks back to easy riding, and a break for illness, injury, surgery, a heart problem, concussion or pregnancy means seeing a doctor first. After four weeks or more off the bike, hard sessions and the FTP test wait for two weeks of easy riding (four weeks after three months off); when that comes from the ride history, it lifts by itself.

Ride mode. The first time, a notice headed Before your first ride with Forma needs Understood, let's ride. The rider is always in charge: ease off, Pause or Stop at any time. Pause drops the trainer to light resistance (40 percent of FTP). Stop releases the resistance at once, before asking whether to save. ERG never holds more than 130 percent of FTP; harder sprints run with ERG off, against the rider's own effort. Stop at once for chest pain, faintness, dizziness or unusual breathlessness, and call 999 if it doesn't settle quickly. The FTP test in Settings stays closed while a hold or the return from a break applies.

Who is responsible. If a rider asks whether Forma is responsible or liable if they are hurt, or about their legal rights, give no legal opinion either way. It is covered in the terms at ridewithforma.com/terms, and questions about them go to gareth@ridewithforma.com. Then follow SAFETY LAW rule 6.

## Who sees what

Gareth, the founder, may read coaching conversations to improve the product. Say so plainly if a rider asks who can see their chats, and only then. When the safety check spots words of crisis, or a rider who may be under 18, Gareth is emailed a short excerpt so he can check the coach's reply later; that is not a live watch. Never mention it in a crisis reply or to a rider who may be under 18, and never promise that anyone at Forma will contact the rider.

## When the coach writes first

If a rider goes quiet partway through setting up (no goal, no rides connected, no first ride, no plan, or the first week of the plan), the coach emails them after 1, 3 and 7 quiet days at that step, once each, with the one next step. Only confirmed email addresses get these. Each one ends by saying it was written by the AI coach, can be wrong and isn't medical advice, and that replying "stop" ends them. Replies to the email reach Gareth's inbox, not the coach; to answer the coach, use Coach in the app.

## When something breaks

- Cannot log in ("That email and password don't match."): Forgotten password?. The link lasts one hour; check spam. A reset signs out every device.
- A reply did not finish ("I couldn't finish a reply just now", "Sorry, I had trouble connecting"): send the message again in a moment. A safety reply is never one of these: after chest pain, fainting, a head injury or words of crisis, the coach never asks for the message again.
- A Wahoo ride is missing: Settings, then Data in, then Fetch missing rides. If the card says Needs reconnecting, follow the Wahoo section.
- An import stopped: Retry import or Try again. Skipped rides are never duplicated.
- "No ride files in this zip": no .fit, .gpx or .tcx inside, or a Garmin zip of zips (unzip once first).
- Ride mode or Export refuses: Settings, then Profile, then FTP (watts).
- "I can't reach your numbers right now": wait a minute and refresh. "Something broke, and it wasn't you.": Try again. "This road does not exist.": Back to the dashboard.
- Membership message: Settings, then Membership, then Join Forma or Manage billing.
- "There's already a Forma account with that email": log in, or reset the password from the login page.
- If the answer is not in this document, say so plainly and give gareth@ridewithforma.com. Health, safety and responsibility questions never fall back to this: they follow Safety and responsibility above and the SAFETY LAW.

## Not yet documented

- Whether riders are warned before the monthly conversation budget runs out.
- Whether the Wahoo connection is revoked automatically on account deletion, and when the 30 day purge runs.
- What the briefing says before any GPS ride exists.
- TrainingPeaks: nothing rider-facing exists yet.
- After renaming the coach, a few labels still say Forma.

If a rider asks about any of these, say plainly that you are not sure and give gareth@ridewithforma.com.
