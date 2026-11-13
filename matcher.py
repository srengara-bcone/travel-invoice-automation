"""
Scoring and one-to-one assignment used to pair receipts with sheet rows.

Both steps (receipt -> master row, master row -> annexure row) work inside one
ER, so the ER has already narrowed the candidates. Amount and date then decide:

    score = 0.6 * amount + 0.4 * date            (accepted from 0.60)

Type is too hard to read from a receipt to count against a match. It only adds a
small bonus (at most 0.05, less than any one step of the amount or date scores)
so it can break a tie between otherwise identical rows, never flip a decision.

An exact amount with a date that doesn't match is still accepted, but only when
it is the single row with that amount: claim dates often differ from the receipt
date, yet two rows with the same amount and no date to separate them are a guess.
"""

import re
from datetime import date
from difflib import SequenceMatcher

W_AMOUNT, W_DATE = 0.6, 0.4
TYPE_BONUS = 0.05
ACCEPT_MIN = 0.60
EPS = 1e-9


def _base(item: str) -> str:
    """Category with seniority/meal-slot wording and the Bus variant folded away."""
    s = re.sub(r"\(.*?\)", "", (item or "").lower())
    s = re.sub(r"\s+-\s+otherthan.*$", "", s).replace("/bus", "")
    return re.sub(r"\s+", " ", s).strip()


_TRANSPORT = {_base(x) for x in (
    "Cab/Shuttle/Train", "Autorickshaw/Bus", "Car Rental", "Toll", "Parking",
    "Personal Car Miles - USA", "Personal Mileage India - 2 wheeler/4 wheeler")}


def type_score(a: str, b: str) -> float:
    if not a or not b:
        return 0.0
    if a.strip().lower() == b.strip().lower():
        return 1.0
    ba, bb = _base(a), _base(b)
    if ba == bb:
        return 0.9
    if ba in _TRANSPORT and bb in _TRANSPORT:
        return 0.35
    return SequenceMatcher(None, ba, bb).ratio() * 0.5


def date_score(a, b) -> float:
    if not a or not b:
        return 0.0
    gap = abs((a - b).days)
    score = 1.0 if gap == 0 else 0.6 if gap == 1 else 0.3 if gap <= 3 else 0.0
    # Day and month are easy to swap when reading a receipt (03/10 vs 10/03).
    if a.day <= 12 and a.day != a.month:
        try:
            if date(a.year, a.day, a.month) == b:
                score = max(score, 0.5)
        except ValueError:
            pass
    return score


def amount_score(x, candidates) -> float:
    """Best closeness between x and any of the candidate amounts."""
    best = 0.0
    for c in candidates:
        if x is None or c is None:
            continue
        diff = abs(x - c)
        rel = diff / max(abs(c), 1.0)
        best = max(best, 1.0 if diff <= 0.01 else 0.8 if rel <= 0.01 else 0.4 if rel <= 0.05 else 0.0)
    return best


def _pairs(n_left: int, n_right: int, features) -> list[tuple[float, float, int, int, bool]]:
    """Acceptable (total, main, left, right, date_mismatch) pairs.
    features(i, j) -> (amount score, date score, type score)."""
    out = []
    for i in range(n_left):
        cands = []
        for j in range(n_right):
            a, d, t = features(i, j)
            if a > 0:
                cands.append((j, a, d, t))
        exact_count = sum(1 for _, a, _, _ in cands if a == 1.0)
        for j, a, d, t in cands:
            main = W_AMOUNT * a + W_DATE * d
            if main < ACCEPT_MIN - EPS:
                continue
            if d == 0 and not (a == 1.0 and exact_count == 1):
                continue  # no date to separate it from other rows with the same amount
            out.append((main + TYPE_BONUS * t, main, i, j, d == 0))
    return out


def _assign(pairs) -> dict[int, tuple[int, float, list[int], bool]]:
    """Greedy best-first one-to-one assignment.
    Returns {left index: (right index, total, rivals, date_mismatch)}. rivals lists the other
    still-free right-hand rows with the same amount/date score, i.e. the rows only the type
    bonus (or nothing) separated from the chosen one; empty means a clear winner."""
    pairs = sorted(pairs, key=lambda p: (-p[0], p[2], p[3]))
    taken_l: set[int] = set()
    taken_r: set[int] = set()
    out: dict[int, tuple[int, float, list[int], bool]] = {}
    for total, main, i, j, mismatch in pairs:
        if i in taken_l or j in taken_r:
            continue
        rivals = [j2 for _, m2, i2, j2, _ in pairs if i2 == i and j2 != j and j2 not in taken_r and abs(m2 - main) < EPS]
        out[i] = (j, total, rivals, mismatch)
        taken_l.add(i)
        taken_r.add(j)
    return out


def match_receipts_to_master(receipts: list[dict], master_rows: list[dict]) -> dict[int, tuple[dict, float, bool, bool]]:
    """receipts: extracted fields (amount, date_obj, type).
    Returns {receipt index: (master row, score, rival master rows, amount_only)}."""
    def features(i, j):
        r, m = receipts[i], master_rows[j]
        if r.get("amount") is None:
            return 0.0, 0.0, 0.0
        return (amount_score(r["amount"], [m["item_amount"], m["claimed"], m["converted"], m["usd"]]),
                date_score(r.get("date_obj"), m["date"]),
                type_score(r.get("type"), m["item"]))

    pairs = _pairs(len(receipts), len(master_rows), features)
    return {i: (master_rows[j], s, [master_rows[x] for x in tie], mismatch) for i, (j, s, tie, mismatch) in _assign(pairs).items()}


def match_master_to_annexure(master_rows: list[dict], annex_rows: list[dict]) -> dict[int, tuple[dict, float, bool]]:
    """Returns {index into master_rows: (annexure row, score, tie)} using USD, date and item."""
    def features(i, j):
        m, a = master_rows[i], annex_rows[j]
        return (amount_score(m["usd"], [a["usd"]]), date_score(m["date"], a["date"]), type_score(m["item"], a["item"]))

    pairs = _pairs(len(master_rows), len(annex_rows), features)
    return {i: (annex_rows[j], s, [annex_rows[x] for x in tie]) for i, (j, s, tie, _) in _assign(pairs).items()}


# ---------------------------------------------------------------------------
# Second pass: elimination
#
# After the strict pass, the receipts still unmatched and the annexure rows still
# unclaimed are paired with looser rules. A row's name goes to one receipt only,
# so this is a much smaller problem than the first pass: a link that would be too
# weak to trust against a whole sheet is enough to choose among a few leftovers.
# ---------------------------------------------------------------------------
LOOSE_MIN = 0.35


def _loose_amount(est, usd) -> float:
    """est is the receipt amount converted to USD (or None when it can't be)."""
    if est is None or usd is None:
        return 0.0
    rel = abs(est - usd) / max(abs(usd), 1.0)
    return 1.0 if rel <= 0.01 else 0.8 if rel <= 0.03 else 0.5 if rel <= 0.10 else 0.2 if rel <= 0.25 else 0.0


def _loose_date(d, a: dict) -> float:
    """Same tiers as date_score, plus: inside the row's From/To range, or within a week."""
    if not d:
        return 0.0
    if a.get("from") and a.get("to") and a["from"] <= d <= a["to"]:
        return 1.0
    s = date_score(d, a["date"])
    if s == 0 and a["date"] and abs((d - a["date"]).days) <= 7:
        s = 0.2
    return s


def _reasons(est, d, receipt_type, a: dict) -> list[str]:
    """Human-readable links between a receipt and an annexure row, for the report."""
    out = []
    if _loose_amount(est, a["usd"]) >= 0.5:
        out.append(f"amount about {abs(est - a['usd']) / max(abs(a['usd']), 1.0):.0%} apart (est. USD {est:,.2f})")
    if d and a.get("from") and a.get("to") and a["from"] <= d <= a["to"]:
        out.append("date inside the From/To range")
    elif _loose_date(d, a) >= 0.5 and a["date"]:
        gap = abs((d - a["date"]).days)
        out.append("same date" if gap == 0 else f"date {gap} day{'s' if gap != 1 else ''} apart")
    if type_score(receipt_type, a["item"]) >= 0.9:
        out.append("type matches")
    return out


def match_leftovers(receipts: list[dict], est_usd: list, annex_rows: list[dict]) -> dict[int, tuple[dict, float, bool, list[str]]]:
    """receipts: extracted fields; est_usd: each receipt's amount in USD (None if unknown);
    annex_rows: the annexure rows no receipt has claimed.
    Returns {receipt index: (annexure row, score, tie, reasons)}.

    A pair needs a real link (amount close, date close, or the same type) and a score of
    LOOSE_MIN; the best pairs are then assigned one-to-one."""
    pairs = []
    for i, r in enumerate(receipts):
        for j, a in enumerate(annex_rows):
            A = _loose_amount(est_usd[i], a["usd"])
            D = _loose_date(r.get("date_obj"), a)
            T = type_score(r.get("type"), a["item"])
            if not (A >= 0.5 or D >= 0.5 or T >= 0.9):
                continue
            score = 0.5 * A + 0.3 * D + 0.2 * T
            if score >= LOOSE_MIN:
                pairs.append((score, score, i, j, False))
    out = {}
    for i, (j, score, tie, _) in _assign(pairs).items():
        a = annex_rows[j]
        out[i] = (a, score, [annex_rows[x] for x in tie], _reasons(est_usd[i], receipts[i].get("date_obj"), receipts[i].get("type"), a))
    return out
