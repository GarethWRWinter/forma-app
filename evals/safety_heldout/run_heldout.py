"""Measure the regex red-flag detector on the blind held-out set.

Calls the detector the way the chat path does for the rider's own message:
coach_service._regex_first -> safety_screen._detect(text) (what
detect_red_flags wraps), then screen_message -> _settled_hits(hits, text).
Cards from safety_screen._cards_for, holds from HOLD_FOR. No DB, no model.
"""
import json
import sys
from collections import Counter, defaultdict

sys.path.insert(0, "/Users/garethwinter/gareth-coaching-api")
sys.path.insert(0, "evals/safety_heldout")

from app.services import safety_screen as s  # noqa: E402
from heldout import ORDINARY, RED_FLAGS  # noqa: E402

# Held-out label -> detector kind.
LABEL_TO_KIND = {"eating": "restriction"}
# A kind is "serious" when it opens a hold or shows a card.
SERIOUS = set(s.HOLD_FOR) | set(s.CARD_FOR_KIND)


def screen(text):
    raw = s.detect_red_flags(text)
    hits = s._settled_hits(s._detect(text), text)
    assert [h["kind"] for h in raw] == [h.kind for h in s._detect(text)]
    cards = s._cards_for(hits, None)
    holds = {h.kind: s.HOLD_FOR[h.kind] for h in hits if h.kind in s.HOLD_FOR}
    return hits, cards, holds


def fmt(hits):
    return ", ".join(f"{h.kind}[{h.severity}]({h.matched!r})" for h in hits) or "(none)"


out = {"red": [], "ordinary": []}

per = defaultdict(lambda: Counter())
for text, label in RED_FLAGS:
    want = LABEL_TO_KIND.get(label, label)
    hits, cards, holds = screen(text)
    kinds = {h.kind for h in hits}
    if want in kinds:
        status = "caught"
    elif kinds & SERIOUS:
        status = "wrong_kind"
    else:
        status = "missed"
    per[label][status] += 1
    out["red"].append(dict(label=label, want=want, status=status, text=text,
                           hits=fmt(hits), cards=[c[2] for c in cards], holds=holds))

for text in ORDINARY:
    hits, cards, holds = screen(text)
    kinds = {h.kind for h in hits}
    if not hits:
        status = "clean"
    elif cards or holds or (kinds & s.ALERT_KINDS):
        status = "hold_or_card"
    else:
        status = "info_only"
    out["ordinary"].append(dict(status=status, text=text, hits=fmt(hits),
                                cards=[f"{c[2]}:{c[0]}" for c in cards], holds=holds))

n = len(RED_FLAGS)
tot = Counter(r["status"] for r in out["red"])
print(f"RED_FLAGS n={n}: caught right kind {tot['caught']} ({tot['caught']/n:.1%}), "
      f"caught wrong serious kind {tot['wrong_kind']}, missed {tot['missed']}; "
      f"any serious alarm {tot['caught']+tot['wrong_kind']} ({(tot['caught']+tot['wrong_kind'])/n:.1%})")
for label in sorted(per):
    c = per[label]
    k = sum(c.values())
    print(f"  {label:13s} {c['caught']}/{k} right ({c['caught']/k:.0%})"
          f"  wrong-kind {c['wrong_kind']}  missed {c['missed']}")

m = len(ORDINARY)
o = Counter(r["status"] for r in out["ordinary"])
print(f"ORDINARY n={m}: clean {o['clean']}, hold/card {o['hold_or_card']} "
      f"({o['hold_or_card']/m:.1%}), info-only {o['info_only']} ({o['info_only']/m:.1%}); "
      f"any hit {o['hold_or_card']+o['info_only']} ({(o['hold_or_card']+o['info_only'])/m:.1%})")

print("\n=== RED FLAG MISSES AND WRONG KINDS ===")
for r in out["red"]:
    if r["status"] != "caught":
        print(f"[{r['status']}] {r['label']}: {r['text']}\n    -> {r['hits']} cards={r['cards']} holds={r['holds']}")

print("\n=== ORDINARY FALSE ALARMS ===")
for r in out["ordinary"]:
    if r["status"] != "clean":
        print(f"[{r['status']}] {r['text']}\n    -> {r['hits']} cards={r['cards']} holds={r['holds']}")

print("\n=== CAUGHT (for reference) ===")
for r in out["red"]:
    if r["status"] == "caught":
        print(f"{r['label']}: {r['hits']}")

with open("evals/safety_heldout/results.json", "w") as f:
    json.dump(out, f, indent=1)
