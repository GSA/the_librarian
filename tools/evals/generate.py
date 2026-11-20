"""Deterministic, seeded known-item and absence case generation from a published index.

A known-item case samples a document (stratified by role, confidence level and rendition
format), picks one of its indexed sections and builds a literal query from that section's
distinctive title terms, then its indexed-text terms (never words of the document path, and
never stopwords or terms in more than ``COMMON`` sections). A query already used, or matching
more than ``SPECIFIC`` sections (near-duplicate documents), is passed over for another section;
a document with no such section within ``SECTION_TRIES`` is skipped and counted. Every query
term comes from the same indexed section, so the document can always be found; whether it ranks
within ``k`` is what a run measures. Generation never checks rank, so a baseline is not tuned to
pass."""

import hashlib
import random
import re

from tools.evals.cases import EvalError, SCHEMA, require, validate

STOPWORDS = frozenset("""
able about above after again against also although among another anyone anything are around
because been before being below between both but came can cannot could did does doing done down
during each either else ever every from further had has have having her here hers herself him
himself his how however into its itself just least less like line made make many may might
more most much must near neither never next none nor not now off often once only onto other
others otherwise our ours ourselves out over own page part per perhaps please rather record
said same see seen several shall she should since some such than that the their theirs them
themselves then there therefore these they this those though through thus till together too
toward under until upon very via was were what whatever when where whether which while who
whom whose why will with within without would yes yet you your yours yourself yourselves
""".split())
TOKEN = re.compile(r"[^\W_]+")
SHAPE = re.compile(r"[a-z][a-z0-9]{3,24}")
HEXLIKE = re.compile(r"[0-9a-f]{8,}")
COMMON = 1000
SPECIFIC = 200
SECTION_TRIES = 25
CONSONANTS = "bcdfghjklmnpqrstvwxz"


def usable(token):
    token = token.lower()
    return (token.isascii() and SHAPE.fullmatch(token) is not None and token not in STOPWORDS
            and sum(c.isalpha() for c in token) >= 4 and HEXLIKE.fullmatch(token) is None)


def candidate_terms(title, text):
    """Usable terms in first-seen order, title terms first, each tagged with its origin."""
    seen = {}
    for origin, value in (("title", title), ("text", text)):
        for token in TOKEN.findall(value or ""):
            token = token.lower()
            if usable(token) and token not in seen:
                seen[token] = origin
    return seen


class Frequencies:
    """Capped matching-section counts, so a common term or query costs a bounded scan."""

    def __init__(self, connection):
        self.connection, self.counts = connection, {}

    def matches(self, terms, cap):
        expression = " AND ".join('"' + term + '"' for term in terms)
        if (expression, cap) not in self.counts:
            self.counts[expression, cap] = self.connection.execute(
                "SELECT count(*) FROM (SELECT rowid FROM search_fts WHERE search_fts MATCH ? LIMIT ?)",
                (expression, cap + 1)).fetchone()[0]
        return self.counts[expression, cap]

    def __call__(self, term):
        return self.matches((term,), COMMON)


def known_item_query(terms, frequency, size):
    """Up to ``size`` distinctive terms: title terms first, then the rarest non-unique terms."""
    scored = [(origin != "title", count == 1, count, term)
              for term, origin in terms.items()
              for count in (frequency(term),) if 1 <= count <= COMMON]
    return [term for *_, term in sorted(scored)[:size]]


def documents(connection):
    rows = connection.execute(
        "SELECT d.path, d.role, c.level, json_extract(r.payload, '$.format') AS format "
        "FROM documents d JOIN document_confidence c ON c.path = d.path "
        "LEFT JOIN rendition_documents r ON r.path = d.path ORDER BY d.path").fetchall()
    strata = {}
    for path, role, level, format_ in rows:
        strata.setdefault((role, level, format_ or "markdown"), []).append(path)
    return strata


def allocate(strata, count):
    """Proportional quotas by largest remainder, at least one per stratum when there is room."""
    total = sum(len(paths) for paths in strata.values())
    keys = sorted(strata)
    if not total or not count:
        return dict.fromkeys(keys, 0)
    count = min(count, total)
    floor = 1 if count >= len(keys) else 0
    quotas = {key: min(len(strata[key]), max(floor, count * len(strata[key]) // total)) for key in keys}
    remainders = sorted(keys, key=lambda key: (-(count * len(strata[key]) % total), key))
    while sum(quotas.values()) < count:
        grown = False
        for key in remainders:
            if sum(quotas.values()) >= count:
                break
            if quotas[key] < len(strata[key]):
                quotas[key] += 1
                grown = True
        if not grown:
            break
    while sum(quotas.values()) > count:
        key = max(keys, key=lambda k: (quotas[k], k))
        quotas[key] -= 1
    return quotas


def sections(connection, path):
    # Locator range on its unique index: the document's own locator plus every "path#..." one.
    return [row[0] for row in connection.execute(
        "SELECT id FROM sections WHERE locator = ? OR (locator >= ? AND locator < ?) "
        "ORDER BY id", (path, path + "#", path + "$"))]


def indexed(connection, section):
    """The exact title and text the search index holds for a section (renditions index a
    search text that differs from their stored record)."""
    return connection.execute("SELECT title, text FROM search_fts WHERE rowid = ?", (section,)).fetchone()


def known_item(connection, path, rng, frequency, size, used):
    rows = sections(connection, path)
    # A document node's title is its path; path words name storage, not content.
    path_terms = {token.lower() for token in TOKEN.findall(path)}
    order = list(range(len(rows)))
    rng.shuffle(order)
    for position in order[:SECTION_TRIES]:
        title, text = indexed(connection, rows[position])
        terms = {term: origin for term, origin in candidate_terms(title, text).items()
                 if term not in path_terms}
        query = known_item_query(terms, frequency, size)
        # A query matching many sections (near-duplicate documents) cannot name one known item.
        if query and " ".join(query) not in used and frequency.matches(query, SPECIFIC) <= SPECIFIC:
            used.add(" ".join(query))
            return " ".join(query)
    return None


def case_id(seed, kind, value):
    return f"gen-s{seed}-{kind}-{value}"


def generate(connection, *, seed, count, absences, k=10, terms=3):
    """Generated cases and a body-free summary of what was sampled and skipped."""
    require(type(seed) is int, "--seed must be an integer")
    require(0 <= count <= 5000 and 0 <= absences <= 1000, "--count is 0..5000 and --absences 0..1000")
    require(1 <= k <= 50 and 1 <= terms <= 8, "--k is 1..50 and --terms 1..8")
    strata = documents(connection)
    quotas = allocate(strata, count)
    frequency = Frequencies(connection)
    used = set()
    generated, summary = [], {"strata": [], "skipped": 0}
    for key in sorted(strata):
        role, level, format_ = key
        paths = list(strata[key])
        random.Random(f"{seed}:{role}:{level}:{format_}").shuffle(paths)
        rng = random.Random(f"{seed}:{role}:{level}:{format_}:sections")
        made = 0
        for path in paths:
            if made >= quotas[key]:
                break
            query = known_item(connection, path, rng, frequency, terms, used)
            if query is None:
                summary["skipped"] += 1
                continue
            made += 1
            generated.append({
                "id": case_id(seed, "known", hashlib.sha256(path.encode()).hexdigest()[:16]),
                "origin": "generated", "query": query, "k": k,
                "tags": ["known-item", f"role:{role}", f"confidence:{level}", f"format:{format_}"],
                "expect_paths": [path], "expect_confidence": level,
                "generator": {"method": "known-item-v1", "seed": seed},
            })
        summary["strata"].append({"role": role, "confidence": level, "format": format_,
                                  "documents": len(strata[key]), "quota": quotas[key], "generated": made})
    rng = random.Random(f"{seed}:absence")
    for number in range(1, absences + 1):
        query = " ".join("".join(rng.choice(CONSONANTS) for _ in range(10)) for _ in range(2))
        generated.append({
            "id": case_id(seed, "absent", f"{number:03d}"), "origin": "generated", "query": query,
            "k": k, "tags": ["absence"], "expect_absent": True, "expect_absence": "absent-corpus",
            "generator": {"method": "absence-v1", "seed": seed},
        })
    summary["known_item"] = len(generated) - absences
    summary["absence"] = absences
    return generated, summary


def merge(existing, generated, seed):
    """Keep authored and other-seed cases; replace this seed's generated cases; refuse id clashes."""
    previous = existing["cases"] if existing else []
    kept = [case for case in previous
            if not (case["origin"] == "generated" and case["generator"]["seed"] == seed)]
    clashes = {case["id"] for case in kept} & {case["id"] for case in generated}
    if clashes:
        raise EvalError(f"Generated case ids collide with kept cases: {', '.join(sorted(clashes)[:5])}")
    return validate({"schema": SCHEMA, "cases": kept + generated}), len(previous) - len(kept)
