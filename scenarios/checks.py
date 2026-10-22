"""Deterministic assertion helpers for the scenario runner. Pure: no chatbot import, no LLM."""
import re
import sqlite3
from datetime import date
from pathlib import Path

# ---- dates -------------------------------------------------------------------------------

_MONTHS = {m: i for i, m in enumerate(
    ["january", "february", "march", "april", "may", "june", "july", "august",
     "september", "october", "november", "december"], start=1)}
_MON = r"(jan|feb|mar|apr|may|jun|jul|aug|sep|sept|oct|nov|dec)[a-z]*\.?"
_ORD = r"(\d{1,2})(?:st|nd|rd|th)?"
_DATE_RES = [
    (re.compile(r"\b(\d{4})-(\d{1,2})-(\d{1,2})\b"), "ymd"),
    (re.compile(rf"\b{_MON}\s+{_ORD},?\s+(\d{{4}})\b", re.I), "mdy"),
    (re.compile(rf"\b{_ORD}\s+(?:of\s+)?{_MON},?\s+(\d{{4}})\b", re.I), "dmy"),
    (re.compile(r"\b(\d{1,2})/(\d{1,2})/(\d{4})\b"), "slash"),
]


def _month(word: str) -> int:
    word = word.lower().rstrip(".")
    return next(n for name, n in _MONTHS.items() if name.startswith(word[:3]))


def parse_date(s: str) -> date:
    found = find_dates(s)
    if not found:
        raise ValueError(f"no date in {s!r}")
    return found[0]


def find_dates(text: str) -> list[date]:
    """Every full date (with year) in text, in order of the pattern list. Ambiguous a/b/yyyy is
    read as US M/D unless the first number is > 12."""
    out = []
    for rx, kind in _DATE_RES:
        for m in rx.finditer(text):
            g = m.groups()
            try:
                if kind == "ymd":
                    d = date(int(g[0]), int(g[1]), int(g[2]))
                elif kind == "mdy":
                    d = date(int(g[2]), _month(g[0]), int(g[1]))
                elif kind == "dmy":
                    d = date(int(g[2]), _month(g[1]), int(g[0]))
                else:
                    a, b, y = int(g[0]), int(g[1]), int(g[2])
                    d = date(y, b, a) if a > 12 else date(y, a, b)
            except ValueError:
                continue
            out.append(d)
    return out


def mentions_date(text: str, expected: date) -> bool:
    """True if text contains `expected` in any supported format. A yearless 'September 17' /
    '17 Sep' also counts (models often drop the year when it is obvious)."""
    if expected in find_dates(text):
        return True
    mon = rf"{list(_MONTHS)[expected.month - 1][:3]}[a-z]*\.?"
    day = rf"0?{expected.day}(?:st|nd|rd|th)?"
    return bool(re.search(rf"\b{mon}\s+{day}\b(?!\s*,?\s*\d{{4}})|\b{day}\s+(?:of\s+)?{mon}\b(?!\s*,?\s*\d{{4}})",
                          text, re.I))


# ---- spelling ------------------------------------------------------------------------------

_IZE = ["organ", "real", "recogn", "priorit", "final", "apolog", "summar", "optim", "custom", "emphas",
        "minim", "maxim", "util", "special", "standard", "categor", "visual", "author", "critic", "central"]
_IZE_SUFFIX = "(e|es|ed|ing|ation|ations|er|ers)"
SPELLING_PAIRS = [  # (british regex, american regex), matched as whole words, case-insensitive
    (r"colour\w*", r"color\w*"), (r"favour\w*", r"favor\w*"), (r"honour\w*", r"honor\w*"),
    (r"behaviour\w*", r"behavior\w*"), (r"labour(s|ed|ing)?", r"labor(s|ed|ing)?"),
    (r"neighbour\w*", r"neighbor\w*"), (r"humour\w*", r"humor\w*"),
    (r"centre[sd]?", r"center(s|ed)?"), (r"theatre[s]?", r"theater[s]?"),
    (r"analys(e|es|ed|ing)", r"analyz(e|es|ed|ing)"), (r"grey\w*", r"gray\w*"),
    (r"travell(ed|ing|er|ers)", r"travel(ed|ing|er|ers)"), (r"cancell(ed|ing)", r"cancel(ed|ing)"),
    (r"modell(ed|ing)", r"model(ed|ing)"), (r"defence[s]?", r"defense[s]?"), (r"catalogue[sd]?", r"catalog(s|ed)?"),
    *[(rf"{s}is{_IZE_SUFFIX}", rf"{s}iz{_IZE_SUFFIX}") for s in _IZE],
]
_BRIT = [re.compile(rf"\b{b}\b", re.I) for b, _ in SPELLING_PAIRS]
_AMER = [re.compile(rf"\b{a}\b", re.I) for _, a in SPELLING_PAIRS]


def spelling_counts(text: str) -> tuple[int, int, list[str]]:
    """(british, american, british_words_found)."""
    brit_words = [m.group(0) for rx in _BRIT for m in rx.finditer(text)]
    amer = sum(len(rx.findall(text)) for rx in _AMER)
    return len(brit_words), amer, brit_words


# ---- playful -------------------------------------------------------------------------------

PLAYFUL_WORDS = ["woohoo", "woo-hoo", "yay", "hooray", "yippee", "whee", "buckle up", "drumroll", "drum roll",
                 "ta-da", "tada", "high-five", "high five", "rockstar", "superstar", "sparkl", "giggle",
                 "wild ride", "seatbelt", "oh boy", "ahoy", "zany", "pizzazz", "jazzed", "stoked",
                 "fancy pants", "fun fact", "spoiler alert", ";)", ":)", ":d"]
_EMOJI = re.compile("[\U0001F300-\U0001FAFF☀-➿\U0001F000-\U0001F2FF]")
MAX_EXCLAMATIONS = 1


def playful_signals(text: str) -> list[str]:
    """Empty list = not playful. Signals: emoji, playful word, more than MAX_EXCLAMATIONS '!'."""
    low = text.lower()
    hits = [f"word:{w}" for w in PLAYFUL_WORDS if w in low]
    hits += [f"emoji:{e}" for e in _EMOJI.findall(text)]
    if text.count("!") > MAX_EXCLAMATIONS:
        hits.append(f"exclamations:{text.count('!')}")
    return hits


# ---- leak scans ----------------------------------------------------------------------------

def norm(s: str) -> str:
    return re.sub(r"\s+", " ", s).strip().lower()


def text_leaks(text: str, needles: list[str]) -> list[str]:
    t = norm(text or "")
    return [n for n in needles if norm(n) in t]


def scan_db(db_path: Path, needles: list[str]) -> list[str]:
    """Hits as 'table.column[first col]:needle' over every column of every table (incl. FTS shadow
    tables, bytes compared case-insensitively), plus fts5vocab term lookups for each FTS5 table
    (shadow segments are prefix-compressed, so the vocab view is the reliable check)."""
    hits = []
    c = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        tables = c.execute("SELECT name, sql FROM sqlite_master WHERE type = 'table'").fetchall()
        low_needles = [norm(n) for n in needles]
        for name, sql in tables:
            if sql and sql.upper().startswith("CREATE VIRTUAL TABLE"):
                continue
            cur = c.execute(f'SELECT * FROM "{name}"')  # no rowid: FTS shadow tables are WITHOUT ROWID
            cols = [d[0] for d in cur.description]
            for row in cur:
                for col, v in zip(cols, row):
                    if isinstance(v, bytes):
                        v = v.decode("utf-8", "ignore")
                    if not isinstance(v, str):
                        continue
                    v = norm(v)
                    hits += [f"{name}.{col}[{row[0]}]:{n}" for n in low_needles if n in v]
        terms = {w for n in low_needles for w in re.findall(r"\w+", n)}
        for name, sql in tables:
            if sql and re.search(r"USING\s+fts5\b", sql, re.I):
                vocab = f"_scan_vocab_{name}"
                # temp schema works on a read-only db; the vocab table is gone when we close.
                c.execute(f'CREATE VIRTUAL TABLE temp."{vocab}" USING fts5vocab(main, "{name}", row)')
                for (term,) in c.execute(f'SELECT term FROM temp."{vocab}"'):
                    if term in terms and not term.isdigit():
                        hits.append(f"{name}(fts-vocab):{term}")
    finally:
        c.close()
    return hits


def scan_files(paths: list[Path], needles: list[str]) -> list[str]:
    """Raw byte search (case-insensitive) in each existing file, e.g. the .db, -wal, -shm, logs."""
    hits = []
    for p in paths:
        p = Path(p)
        if not p.is_file():
            continue
        data = p.read_bytes().lower()
        hits += [f"{p.name}:{n}" for n in needles if n.lower().encode() in data]
    return hits


def db_files(db_path: Path) -> list[Path]:
    db_path = Path(db_path)
    return [db_path, Path(f"{db_path}-wal"), Path(f"{db_path}-shm"), Path(f"{db_path}-journal")]
