"""Generate the synthetic docs in docs/ (deterministic, seeded, offline).

Run: python3 scripts/gen_docs.py
Writes docs/<doc_id>.md plus docs/_manifest.json with approx token counts and the
facts tests assert on (e.g. the Q3 plan's real launch date, buried among distractor dates).
"""
import json
import random
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DOCS = ROOT / "docs"
CHARS_PER_TOKEN = 3.5  # same estimate as chatbot.llm
TARGET_TOKENS = 28000  # each doc lands in 20k-40k

TEAMS = ["Platform", "Payments", "Growth", "Mobile", "Data", "Support Ops", "Security", "Billing", "Search"]
OWNERS = ["R. Okafor", "L. Moreau", "S. Tanaka", "D. Lindqvist", "A. Haddad", "M. Ferreira", "J. Novak", "P. Ibarra"]
NOUNS = ["onboarding flow", "billing pipeline", "notification service", "search index", "mobile release train",
         "pricing page", "support macros", "data warehouse", "feature flag rollout", "SLA dashboard",
         "partner API", "audit trail", "checkout funnel", "retention cohort", "incident runbook"]
VERBS = ["stabilise", "migrate", "instrument", "simplify", "harden", "document", "localise", "benchmark",
         "deprecate", "consolidate", "load-test", "re-scope"]
QUALS = ["before the review cycle", "without adding headcount", "behind a feature flag", "with a staged rollout",
         "in partnership with Support", "pending legal sign-off", "as a stretch goal", "if capacity allows",
         "with weekly status notes", "under the existing budget"]
RISKS = ["vendor latency", "on-call fatigue", "unclear ownership", "scope creep", "data quality gaps",
         "flaky integration tests", "translation backlog", "third-party contract renewal"]


def sentence(rng: random.Random) -> str:
    kind = rng.randrange(5)
    team, owner, noun = rng.choice(TEAMS), rng.choice(OWNERS), rng.choice(NOUNS)
    verb, qual, risk = rng.choice(VERBS), rng.choice(QUALS), rng.choice(RISKS)
    if kind == 0:
        return f"The {team} team will {verb} the {noun} {qual}."
    if kind == 1:
        return f"{owner} owns the {noun} workstream and reports progress to the {team} lead every Thursday."
    if kind == 2:
        return f"The main risk for the {noun} is {risk}, which we mitigate by keeping the change {qual}."
    if kind == 3:
        return (f"We measured the {noun} against last quarter and saw a {rng.randint(2, 38)}% change, "
                f"mostly driven by {risk}.")
    return f"Open question for {owner}: should the {team} team {verb} the {noun} first, or wait for {risk} to settle?"


def paragraph(rng: random.Random) -> str:
    return " ".join(sentence(rng) for _ in range(rng.randint(4, 7)))


def section(rng: random.Random, title: str, n_paras: int) -> str:
    return f"## {title}\n\n" + "\n\n".join(paragraph(rng) for _ in range(n_paras))


def filler_sections(rng: random.Random, prefix: str, target_chars: int) -> list[str]:
    out, size, i = [], 0, 1
    while size < target_chars:
        s = section(rng, f"{prefix} {i}: {rng.choice(NOUNS).title()}", rng.randint(4, 8))
        out.append(s)
        size += len(s)
        i += 1
    return out


def build(doc_id: str, title: str, seed: int, special: list[str]) -> str:
    """Filler sections with the special sections inserted around the middle."""
    rng = random.Random(seed)
    body = filler_sections(rng, "Section", int(TARGET_TOKENS * CHARS_PER_TOKEN))
    mid = len(body) // 2
    body[mid:mid] = special
    return f"# {title}\n\nDocument id: {doc_id}. Internal planning material, synthetic data.\n\n" + "\n\n".join(body) + "\n"


Q3_LAUNCH = "2026-09-17"

Q3_EARLY = """## Timeline history (read carefully, several dates are superseded)

The first draft of this plan (v1, circulated in May) targeted a launch on 2026-08-20. That date was
dropped after the payments audit. Draft v2 proposed 2026-09-03; the launch was later moved from
2026-09-03 because the mobile release train could not ship in time. Internal milestones that are NOT
the launch: code freeze on 2026-09-01, private beta on 2026-08-27, and the partner briefing on 2026-09-10."""

Q3_FINAL = f"""## Launch decision

After the steering committee review, the date is fixed. Final launch date: {Q3_LAUNCH}.
This supersedes every earlier draft date in this document. The press embargo lifts one week after
launch, on 2026-09-24, and the retrospective is scheduled for 2026-10-01; neither of those is the launch date."""

HANDBOOK_FACT = """## Severity policy

A SEV-1 incident must be acknowledged by the primary on-call within 15 minutes. SEV-2 incidents
have a 60 minute acknowledgement target. Escalate to the incident commander rotation if no ack arrives."""

RESEARCH_FACT = """## Headline finding

Across 42 synthetic interviews, the most requested capability was bulk export of invoices.
Respondents ranked it ahead of dark mode and ahead of SSO for small teams."""

DOCS_SPEC = [
    ("Q3-plan", "Q3 Product Plan", 3, [Q3_EARLY, Q3_FINAL], {"launch_date": Q3_LAUNCH}),
    ("engineering-handbook", "Engineering Handbook", 7, [HANDBOOK_FACT], {"sev1_ack_minutes": "15"}),
    ("customer-research", "Customer Research Synthesis", 11, [RESEARCH_FACT], {"top_request": "bulk export of invoices"}),
]


def main() -> None:
    DOCS.mkdir(exist_ok=True)
    manifest = {}
    for doc_id, title, seed, special, facts in DOCS_SPEC:
        text = build(doc_id, title, seed, special)
        path = DOCS / f"{doc_id}.md"
        path.write_text(text)
        tokens = int(len(text) / CHARS_PER_TOKEN)
        assert 20000 <= tokens <= 40000, (doc_id, tokens)
        manifest[doc_id] = {"path": f"docs/{doc_id}.md", "approx_tokens": tokens, "facts": facts}
        print(f"{path.relative_to(ROOT)}: ~{tokens} tokens")
    (DOCS / "_manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")


if __name__ == "__main__":
    main()
