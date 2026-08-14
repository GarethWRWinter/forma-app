---
name: forma-brand
description: FORMA visual brand and design system reference. Use whenever building or editing FORMA UI — app screens, components, landing pages, mockups, charts, emails. Trigger on "design", "restyle", "new page", "component", "chart colors", "brand", or any FORMA visual work. Guarantees new work matches the FORMA design system.
---

# FORMA brand v2 — the design system

FORMA reads like print, not pixels: a chalk page, paper sheets laid on it, ink structure, one flamme accent, mono data. Bold Expanded type, hairline rules, flat surfaces, purposeful motion. The wordmark is FORMA followed by **the kite** — a flamme pennant full stop (never a round dot).

Canonical source: `frontend/docs/brand/FORMA-Brand-v2-source.html` (Gareth's brand doc) + delta map in `frontend/docs/brand/BRAND-V2-AUDIT.md`.

## The kite (the mark)

- CSS-drawn: `clip-path: polygon(0 0, 100% 0, 50% 100%)`, always flamme, tip on the baseline. Utility `.f-kite` (size via `--kite`); components `FormaMark` (wordmark + kite) and `Kite`.
- Never an arrow, never rotated, never outlined, never any colour but flamme.
- Type inside the kite only at display sizes ≥64px.
- One kite moment per screen (list markers / sign-offs / page furniture count).
- **The one sanctioned exception: the cadence spinner** (`CadenceSpinner`, 12 Aug 2026). The kite rides a faint crank circle as the coach's working indicator, nose tangential to the path so it leads the direction of travel, turning with pedal-stroke easing (quick through the downstroke, easing over the dead spots). Permitted because here the kite is a moving part, not a mark. It does not count against the one-kite-per-screen rule, and no other animated use of the kite is approved.

## The six inks (tokens in `frontend/src/app/globals.css` @theme, names `vb-*`)

- **Chalk** `#F0F0EC` — **THE PAGE.** `vb-bg`, the ground of every screen.
- **Paper** `#FAFAF7` — sheets laid on the page: cards, inputs, panels (`vb-surface`). White `#FFFFFF` = `vb-surface-raised` only.
- **Ink** `#0B0B0C` — text, strong rules, the wordmark. Dim `#55554F`, muted/slate `#9A9A94`.
- **Slate** `#9A9A94` — muted text, quiet data.
- **Carbon** `#101012` + raised `#1A1A1E` — ride mode / instrument surfaces only.
- **Flamme** `#FF3D00` (`vb-red`) — THE accent, one thing per screen. Bright `#FF5A2A`, dim `#D93400`.
- Borders: subtle `#ECEBE6`, hairline `#D8D8D2`, strong = ink. Recessed fills `vb-sunken` `#E7E7E1`.
- **Zone inks v2** z1→z7 (cold→hot): `#8E9196 #4A72AE #439D7C #D9AC34 #E86F22 #D92420 #B81743`. **Data colour only, never brand colour; flamme is not a zone.**
- Semantic: success `#439D7C`, warning `#D9AC34`.

## Type — three voices

- **The Announcer**: Archivo **Expanded** (`font-stretch: 125%`, loaded via next/font `axes: ["wdth"]`), weights 700–900 (`.f-display`). Headlines, the wordmark, numbers that shout. Never font-light at display sizes.
- **The Coach**: Inter Tight 400/500. Body, guidance, everything Forma says.
- **The Timekeeper**: IBM Plex Mono (`.f-kicker` eyebrows 11px/0.16em/uppercase; `.f-data` tabular-nums). Data is mono; trends are mono `▲▼` glyphs, never coloured pills.
- **Signature**: Caveat, flamme, only for Forma's handwritten sign-off (`.f-signature`).

## The coach — one Forma, two channels

Forma is the single coach (the DS). Two named channels, kept distinct:
- **Race Radio** — Forma's live voice *during a training session* (carbon ride player); terse, in-effort cues.
- **Coach Forma** — the *conversation off the bike* (nav: COACH); the memory-building, goal-working relationship.
Never rename the conversation "Race Radio" (reserved for the in-ride voice), and never add a second coach persona name — always Forma.

## The coach presence

Forma is a **flamme dot**, no avatar, no face, no disc. `CoachDot` in `components/ui/coach-glyph.tsx`:
- `still` (C1) — present, listening. `pulsing` (C2) — thinking/typing. `rippling` (C3) — speaking, **carbon surfaces only**.
- Legacy `CoachGlyph` import still works (renders the still dot centred in the old box).

## Brand imagery — the Abstract (ink-dispersion terrain)

The second imagery register beside hard photography: **iconic climbs rendered as ink-in-water abstractions** (exemplar: Sa Calobra, its 26 stacked switchbacks read as flow and sediment). Source set: `frontend/public/inkscapes/` (Ventoux, Stelvio, Sa Calobra, Tourmalet + two road-art plates), rotated daily in the app via `lib/dailyInkscape.ts`.
- **IN THE APP this register IS the imagery** — the TODAY hero is a daily inkscape with the daily quote overlaid (chalk display text on a dark scrim, IB2 lockup, mono kite-marked attribution).
- **Orbs are editorial-only** (blog, covers, merch, guidelines) — they no longer appear inside app surfaces (decision 2026-07-25).
- Lockup over the art: **chalk wordmark + flamme kite** (IB2, the approved treatment; all-chalk IB1 is the quiet fallback). Never ink-on-ink.
- The lockup sits in the **quiet corner**, clear of the busiest ink.
- Caption pattern: mono kicker, `SA CALOBRA · 26 BENDS`.
- Each piece stays in a limited palette (the Sa Calobra terrain is greens + subtle greys); one climb, one field, no collaging registers.

## Rules of composition ("print, not pixels")

- Flat. No shadows, no gradients, no glows. Radius 2px (sm) / 4px (md) max — never rounded-lg.
- Hairline borders separate; a 2px ink rule (`.f-rule`) opens sections.
- Flamme is scarce: one hot element per screen.
- Whitespace generous; editorial numbered sections (`01 ·`) only when content is truly sequential.
- Carbon (`.f-carbon`) reserved for the live ride player and ride-mode surfaces.
- Nav IA: **TODAY · COACH · RIDES · FORM · PLAN** (+ Brain, Goals, Settings as deeper rooms), mono single words.

## Motion (in globals.css, reduced-motion safe)

- `.f-rise` / `.f-stagger` entrance (fade + 12px rise, 60ms stagger).
- `.f-lift` hover (-2px + border-strong) for clickable cards; `.f-press` active scale.
- `.f-pulse-dot` flamme breathing for live elements; `.f-draw` rule wipe-in; coach dot states animate via `data-state`.
- `useCountUp` for data tiles. Motion is purposeful and brief, never decorative loops.

## Component kit (`frontend/src/components/ui/`)

`FormaMark` (wordmark + kite) · `Kite` · `CoachDot` (+legacy `CoachGlyph`) · `Button` (ink / flamme / ghost / quiet / carbon; mono uppercase; `Arrow` slides) · `Card` (`rail` = flamme left rail, `lift`) · `Kicker` · `SectionHeader` · `DataTile` (count-up, `hot` state, mono `▲▼` deltas) · `EmptyState` (Forma speaks + one CTA) · `ZoneChip` · `ProgressSteps` (mono numbers) · `CoachNote` (the canonical Forma-speaks block: rail + prose + Caveat signature) · `Badge` · `Input` · `Orb` (signed orb cutouts at editorial scale ≥120px, `.f-orb` float shadow, never behind a glow).

## Data-viz (`frontend/src/lib/palette.ts` — never hardcode data colours)

- `SERIES`: ink = primary line, grey = context, flamme = the story (TSB, actual-vs-plan), amber = HR, hairline grids. Thin strokes (1.5px), no area fills, mono tick labels.
- `ZONES` (v2 ramp) + `ZONE_BLOCKS` for intensity; `STEPS` for workout profiles; `BRAIN` for the memory graph (You=ink, Goals=flamme, Values=green, Habits=blue, Insights=gold, People=crimson, Rides=orange, Health=grey, Life=slate, Rules=carbon — all from the v2 ramp).

## Don'ts

The round-dot full stop (v1, dead — use the kite via `FormaMark`) · paper as the page ground (chalk is the page) · the v1 zone ramp (`#4A6FA5 #3E8E7E #D9A62E…`) · zone inks on brand surfaces · soft Scandi cards, forest green `#36513F`, clay `#BB6647`, linen `#F3F1EA` (ALMANAC is dead) · font-light display type · shadows/gradients · flamme everywhere · coloured trend pills (mono ▲▼ only) · centred body text walls · emoji in UI · coach avatars/faces (the coach is the dot).

For words, load the `forma-voice` skill. Fuller reference: the brand guidelines artifact (claude.ai/code/artifact/413c5b72-1247-4eed-9084-70ed9f58944e) + `frontend/docs/brand/`.
