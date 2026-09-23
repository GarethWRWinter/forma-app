"""Memory taxonomy, the canonical entity types of Forma's brain.

The definitions below are LOAD-BEARING: they are rendered directly into the
extraction LLM's prompt (docstrings-are-prompts, per TIE Memory v2). The
"distinct from" prose is what makes classification accurate. Change these and
extraction behaviour changes, treat edits like prompt changes and eval them.

Rules of the taxonomy:
- Tags, not types: a subtype goes in `kind`, a new type only exists when it is
  extracted differently AND holds different data.
- Every entity carries `life_area` (training|body|mind|life) and `visibility`
  (private by default; health is never auto-surfaced).
- Plans are wiring, not a type, a plan is a traversal of a Goal's edges.
"""

ENTITY_TYPES: dict[str, dict] = {
    "value": {
        "prompt": (
            "A belief, identity statement, or principle the user holds. WHY they "
            "ride and how they see themselves. 'Prove something to myself', 'family "
            "comes first', 'never DNF'. Distinct from Goal: a value has no finish "
            "line. Distinct from Insight: a value is held, not learned."
        ),
        "kinds": ["value", "identity", "principle"],
    },
    "goal": {
        "prompt": (
            "A target the user works toward: a race, an event, a number, a dream. "
            "'Manchester→London sub-12', 'get back to 3.9 W/kg'. Distinct from "
            "Value: a goal can be achieved. Distinct from Habit: a goal is an "
            "outcome, not a repeated action."
        ),
        "kinds": ["a_race", "b_race", "dream", "comeback", "number"],
    },
    "gap": {
        "prompt": (
            "A weakness, avoidance, fear, or blind spot inferred about the rider. "
            "the engine of coaching. 'Fades in the final hour', 'avoids intervals', "
            "'nervous on fast descents', 'guilty about rest weeks'. Distinct from "
            "HealthSignal: a gap is a performance/behaviour pattern, not a medical "
            "fact. Only extract when evidence supports it."
        ),
        "kinds": ["physiological", "technical", "psychological", "pacing", "fueling", "scheduling"],
    },
    "insight": {
        "prompt": (
            "A specific piece of coaching advice given or a learning surfaced that "
            "the user could act on. 'Fuel every 45 minutes on long rides', 'cap the "
            "first 5 minutes of climbs at tempo'. Distinct from Value: an insight is "
            "new and actionable. Track lifecycle in status (noted→applied→became_habit)."
        ),
        "kinds": ["fueling", "pacing", "recovery", "technique", "mindset", "equipment"],
    },
    "habit": {
        "prompt": (
            "A recurring practice on a cadence the user does or commits to. "
            "'Tuesday intervals', 'bottle alarm every 45 min', 'screens off at 10pm'. "
            "Distinct from Insight: a habit is being executed repeatedly, not just advised. "
            "Distinct from Goal: ongoing, no finish line."
        ),
        "kinds": ["training", "fueling", "recovery", "lifestyle"],
    },
    "life_event": {
        "prompt": (
            "A dated life circumstance that shapes training: constraints and moments. "
            "'Wedding in Cornwall in June', 'school runs Tue & Thu', 'work crunch "
            "until month end', 'the Cornwall bonk ride'. Distinct from Goal: not an "
            "aspiration. Include recurring constraints as kind=constraint."
        ),
        "kinds": ["constraint", "event", "milestone", "memory"],
    },
    "person": {
        "prompt": (
            "A named human in the user's life other than the user: partner, kids, "
            "coach, physio, clubmates, rivals. Record the relationship. Distinct "
            "from a passing mention: only extract people who matter recurringly."
        ),
        "kinds": ["family", "friend", "rival", "professional", "clubmate"],
    },
    "health_signal": {
        "prompt": (
            "A health fact: injury, illness, sleep pattern, stress, medication. "
            "'Knee twinge week of 22 March', 'sleeping ~6h during launch crunch'. "
            "ALWAYS visibility=private. Distinct from gap: a health signal is a "
            "physical/medical fact, not a performance pattern."
        ),
        "kinds": ["injury", "illness", "sleep", "stress", "medical"],
    },
    "procedural": {
        "prompt": (
            "How the user wants Forma to behave, preferences and rules about the "
            "coaching itself. 'Be direct, no fluff', 'don't message on Sundays', "
            "'explain the why behind sessions'. strength=hard means always obey; "
            "soft means default."
        ),
        "kinds": ["preference", "rule"],
    },
    "ride_memory": {
        "prompt": (
            "A specific memorable ride referenced with meaning attached, an epic, "
            "a disaster, a breakthrough. 'The Cornwall bonk', 'the day I dropped "
            "Dave on Winnats'. Link source_ref to the ride id when inferable. "
            "Distinct from life_event: it is a ride."
        ),
        "kinds": ["epic", "breakthrough", "setback", "race"],
    },
}

EDGE_TYPES: list[str] = [
    "grounds",        # value → goal
    "serves",         # habit → goal
    "surfaces",       # goal/ride → gap
    "addressed_by",   # gap → insight
    "became",         # insight → habit
    "involves",       # anything → person
    "constrains",     # life_event → goal/habit
    "about",          # anything → anything (weak association)
]

LIFE_AREAS = ["training", "body", "mind", "life"]


def extraction_prompt() -> str:
    """Render the taxonomy as the extraction system prompt (stable, cacheable)."""
    lines = [
        "You extract memory entities from a cycling coaching conversation or ride debrief.",
        "You are building the athlete's long-term memory graph. Precision over recall:",
        "extract only durable facts worth remembering for months, never small talk,",
        "never one-off logistics, never the assistant's own filler.",
        "",
        "ENTITY TYPES (choose exactly one `type` per entity):",
    ]
    for name, spec in ENTITY_TYPES.items():
        kinds = "|".join(spec["kinds"])
        lines.append(f"- {name} (kind: {kinds}): {spec['prompt']}")
    # The Rider Dossier: typed personal knowledge that powers
    # hyper-personalization. Extracted alongside the graph in one pass.
    from app.models.dossier import DOSSIER_DIMENSIONS

    lines += [
        "",
        f"EDGE TYPES: {', '.join(EDGE_TYPES)}",
        f"LIFE AREAS (`life_area`, required): {', '.join(LIFE_AREAS)}",
        "",
        "DOSSIER DIMENSIONS, separately, capture personal facts the RIDER reveals",
        "about themselves on these dimensions (only when genuinely stated, never inferred",
        "from the assistant's words):",
    ]
    for dim, desc in DOSSIER_DIMENSIONS.items():
        lines.append(f"- {dim}: {desc}")
    lines += [
        "",
        "Return STRICT JSON, no prose:",
        '{"entities": [{"type": "...", "kind": "...", "life_area": "...", "label": "short human label, max 60 chars", "summary": "1 sentence, optional"}],',
        ' "edges": [{"from_label": "...", "to_label": "...", "edge_type": "..."}],',
        ' "dossier": [{"dimension": "one of the dossier dimensions", "content": "the fact in one plain sentence, rider-specific", "confidence": 0.5-1.0}]}',
        "",
        "Edges may reference entities extracted now OR the KNOWN ENTITIES list provided.",
        "If nothing durable was said, return {\"entities\": [], \"edges\": [], \"dossier\": []}.",
    ]
    return "\n".join(lines)
