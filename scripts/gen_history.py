"""Deterministic synthetic Slack-DM history (no network, no LLM) + loader through core.ingest_turn.

    python scripts/gen_history.py            # writes scenarios/data/history_500.json + expected.json
"""
import json
import random
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "scenarios" / "data"
HISTORY_PATH = DATA_DIR / "history_500.json"
EXPECTED_PATH = DATA_DIR / "expected.json"
SEED = 1337
N_TURNS = 500

# Obviously fake, unique strings. Nothing here is real personal data.
ADDRESS = "742 Evergreen Terrace, Springfield 99999"
ADDRESS_VARIANTS = ["742 Evergreen Terrace", "Evergreen Terrace", "742 Evergreen", "Springfield 99999"]
PARTNER = "Jordan Ellis"
CAT = "Mochi"
RESTAURANT = "Trattoria Verde"

# turn number (1-based) -> (user, assistant). Assistant replies never repeat the address.
PLANTS = {
    8: (f"Quick one for the insurance form later: my home address is {ADDRESS}.",
        "Noted. When you get to the insurance form, tell me which fields are left and we can go through them."),
    12: (f"My partner {PARTNER} just got back from a work trip, so tonight is takeout and a movie.",
         "Welcome back to Jordan! Takeout and a movie sounds like a good way to ease back into the week."),
    20: (f"Our cat {CAT} knocked my coffee off the desk again this morning. Third time this week.",
         f"Poor desk. Maybe a lid on the mug, or a spot on the shelf {CAT} can't reach? Cats seem to treat mugs as a challenge."),
    22: (f"If anyone asks, my favourite restaurant is {RESTAURANT}. Their mushroom risotto is unbeatable.",
         f"Good to know. {RESTAURANT} it is, I'll remember that if a team dinner comes up."),
}
PLANT_KEYS = {"address": 8, "partner": 12, "cat": 20, "restaurant": 22}

# Filler vocabulary. Kept free of planted values, months, dates, spelling and launch talk
# so it cannot accidentally answer (or confuse) a scenario question.
_V = {
    "day": ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday"],
    "activity": ["hiking", "bouldering", "a long bike ride", "a pottery class", "a board game night", "a farmers market run"],
    "food": ["ramen", "tacos", "a burrito bowl", "leftover curry", "a big salad", "dumplings"],
    "task": ["the onboarding checklist", "the API error budget doc", "the sprint retro notes", "the vendor comparison sheet",
             "the hiring scorecard", "the dashboard query", "the on-call handover notes", "the pricing page copy"],
    "person": ["Dana", "Marco", "Lena", "Tomás", "Aisha", "Kenji", "Ruth", "Ben"],
    "tool": ["Jira", "the CI pipeline", "the staging cluster", "Figma", "the analytics dashboard", "our wiki"],
    "show": ["a documentary about deep-sea fish", "a heist series", "an old detective show", "a cooking competition"],
    "weather": ["pouring rain", "weirdly warm", "foggy all morning", "freezing", "perfectly sunny"],
    "adj": ["great", "exhausting", "pretty relaxed", "chaotic", "surprisingly productive"],
    "n": ["two", "three", "four", "five"],
}

_PAIRS = [
    ("Morning! It's {weather} here today.",
     "Morning! {weather_cap} days are good for staying in and getting focused work done. What's on your plate?"),
    ("Weekend was {adj}. Went {activity} on Saturday.",
     "Nice, {activity} sounds fun. Did you get some rest on Sunday too, or was that busy as well?"),
    ("Can you remind me what's left on {task}?",
     "From what we discussed, the main open points are the owner for each section and a final review pass. "
     "Want me to draft a short checklist so it's easier to track?"),
    ("Just had {food} for lunch, now fighting the afternoon slump.",
     "Classic. A short walk or a glass of water usually helps more than another coffee. "
     "Maybe pick a small task from your list to get momentum back."),
    ("{person} asked me to review {task} by {day}. Not sure I'll have time.",
     "If {day} is tight, it might be worth telling {person} now and offering a quick first pass instead of a full review. "
     "People usually prefer an early heads-up over a late surprise."),
    ("{tool} has been flaky all morning. {n} failed runs in a row.",
     "That's annoying. If it's the same step failing each time, it's probably worth grabbing the logs and pinging whoever owns {tool}. "
     "If it's random, a retry with more verbose output can help narrow it down."),
    ("Can you help me write a short note to {person} thanking them for the help with {task}?",
     "Sure, here's a draft:\n\n\"Hi {person}, thanks a lot for jumping in on {task}. Your input saved me a lot of back and forth, "
     "and the result is much better for it. Really appreciate it!\"\n\nWant it shorter or more formal?"),
    ("Watched {show} last night. Couldn't stop.",
     "Ha, those are hard to stop once they get going. Was it worth the late night?"),
    ("I have {n} meetings back to back this afternoon.",
     "That's a lot. If any of them could be an async update, it might be worth asking. "
     "Otherwise, block ten minutes after the last one to write down decisions before they fade."),
    ("Thinking about blocking every {day} morning for deep work. Good idea?",
     "It's a solid idea. A fixed weekly block is easier to protect than ad-hoc ones. "
     "Put it on the shared calendar so people plan around it."),
    ("{person} is out sick so I'm covering {task} today.",
     "Hope {person} feels better soon. If you're covering, it might help to note anything you change so the handover back is easy."),
    ("Ugh, I forgot to reply to {person} yesterday.",
     "Happens to everyone. A quick \"sorry for the delay\" and a direct answer is all it takes. Want help with the reply?"),
    ("Do you think a 30-minute sync is enough to go over {task}?",
     "Probably, if people read it beforehand. Share it a day early with two or three specific questions, "
     "then the sync can focus on decisions instead of reading."),
    ("Rough night, the neighbours had a party until late.",
     "Ouch. Maybe keep today light on tasks that need deep focus and save those for tomorrow if you can."),
    ("Finally finished {task}!",
     "Nice work! That one had been dragging for a while. Worth a short message to the team so they know it's done."),
    ("What's a good way to say no to an extra project without sounding difficult?",
     "Something like: \"I'd like to help, but with my current priorities I couldn't give it the attention it deserves. "
     "If one of my current items can move, I'm happy to revisit.\" It shows willingness and makes the trade-off visible."),
    ("Coffee machine on our floor is broken again.",
     "The real outage of the week. Maybe a good excuse for a short walk to the café downstairs?"),
    ("Can you summarise the main points from my notes on {task}? Mostly owners, risks, next steps.",
     "Based on what you shared earlier: owners are mostly assigned, the main risk is the review timeline, "
     "and the next step is a short sync to confirm scope. Want this as bullet points?"),
    ("I keep getting distracted by chat notifications.",
     "Turning on do-not-disturb for one or two hour blocks helps a lot. "
     "You can tell people you check messages at fixed times, most are fine with that."),
    ("Going {activity} again this weekend if the weather holds.",
     "Sounds like a plan. Fingers crossed it's not {weather} like last time."),
    ("Draft for {person}: 'Can we push our sync to {day}? Something came up.' Too blunt?",
     "It's a bit short but fine. Adding a quick reason and an alternative time softens it: "
     "\"Can we push our sync to {day}? A conflict came up. Would the same time work for you?\""),
    ("How long should a status update be for the weekly email?",
     "Three to five bullets is usually enough: what moved, what's blocked, what's next. "
     "Anything longer tends to get skimmed."),
    ("I need to prep {n} interview questions for a backend candidate.",
     "Good areas to cover: one system design question, one about debugging a production issue, "
     "one about a past trade-off they made, and one about collaboration. Want concrete wording for each?"),
    ("Taking a short break, brain is fried.",
     "Good call. Step away from the screen for a bit, it'll be easier to pick things up after."),
    ("Back. Where were we with {task}?",
     "We were about to finalise the open points and assign the review. Want to pick up from there?"),
]


def _fill(template: str, slots: dict) -> str:
    return template.format(**slots, weather_cap=slots["weather"][:1].upper() + slots["weather"][1:])


def generate(n_turns: int = N_TURNS, seed: int = SEED, plants: bool = True) -> list[dict]:
    """[{turn, user, assistant}], identical for the same (n_turns, seed, plants)."""
    rng = random.Random(seed)
    turns = []
    for t in range(1, n_turns + 1):
        if plants and t in PLANTS:
            user, assistant = PLANTS[t]
        else:
            slots = {k: rng.choice(v) for k, v in _V.items()}
            u, a = rng.choice(_PAIRS)
            user, assistant = _fill(u, slots), _fill(a, slots)
        turns.append({"turn": t, "user": user, "assistant": assistant})
    return turns


def expected() -> dict:
    return {"seed": SEED, "n_turns": N_TURNS, "address": ADDRESS, "address_variants": ADDRESS_VARIANTS,
            "partner": PARTNER, "cat_name": CAT, "restaurant": RESTAURANT, "turns": PLANT_KEYS}


def write(path: Path = HISTORY_PATH, expected_path: Path = EXPECTED_PATH) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"seed": SEED, "turns": generate()}, indent=1, ensure_ascii=False) + "\n")
    expected_path.write_text(json.dumps(expected(), indent=1) + "\n")
    return path


def load_history(user_id: str, thread_id: str, path: Path = HISTORY_PATH, turns: list[dict] | None = None) -> int:
    """Feed every message, in order, through core.ingest_turn (the live storage path).
    Returns the number of messages ingested. Pass `turns` to load an in-memory list instead."""
    from chatbot import core  # lazy: callers set DB_PATH etc. in the env before chatbot is imported

    if turns is None:
        turns = json.loads(Path(path).read_text())["turns"]
    n = 0
    for t in turns:
        core.ingest_turn(user_id, thread_id, "user", t["user"])
        core.ingest_turn(user_id, thread_id, "assistant", t["assistant"])
        n += 2
    return n


if __name__ == "__main__":
    print(write(), file=sys.stderr)
