"""
Rename receipts to the Text value from the Travel Annexure.

Folder layout (the PO number is the folder name):

    4200007104/
        ER27003171115694/  receipt1.pdf  receipt2.jpg ...
        ER27003171115352/  ...

For every receipt: read type, amount and date -> find its row in the master dump
(DATA sheet) for USD -> find the matching row in the annexure sheet that the
Overview sheet assigns to the PO -> copy the receipt under that row's Text.
Anything unclear is copied to NeedsReview and listed in the report; a weaker guess
(found by elimination) is only suggested and waits for the user's approval. Originals
are never modified.

Usage:
    python rename_receipts.py PO_FOLDER --master master.xlsx --annexure annexure.xlsx --out OUTPUT_DIR
"""

import argparse
import hashlib
import json
import re
import shutil
import statistics
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import openpyxl

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

try:
    from dotenv import load_dotenv
    load_dotenv(HERE / ".env")
except ImportError:
    pass

from extractor import explain_error, extract_receipt  # noqa: E402
from matcher import match_leftovers, match_master_to_annexure, match_receipts_to_master, type_score  # noqa: E402
from sheets import load_annexure, load_master, to_date  # noqa: E402

RECEIPT_SUFFIXES = {".pdf", ".jpg", ".jpeg", ".png", ".webp", ".gif"}
REVIEW_DIR = "NeedsReview"
STATUS_RENAMED = "Renamed"
STATUS_SUGGESTED = "Suggested"   # found by elimination: never renamed until the user approves


def is_renamed(row: dict) -> bool:
    return row.get("Status", "").startswith(STATUS_RENAMED)


def currency_rates(master_rows) -> dict[str, float]:
    """Median USD per unit of each non-USD currency, from rows that carry both amounts.
    Used to estimate a receipt's USD amount when its master row can't be found."""
    ratios: dict[str, list[float]] = {}
    for m in master_rows:
        cur, amt, usd = m.get("currency"), m.get("item_amount"), m.get("usd")
        if cur and cur != "USD" and amt and usd and amt > 0 and usd > 0:
            ratios.setdefault(cur, []).append(usd / amt)
    return {cur: statistics.median(v) for cur, v in ratios.items()}


def estimate_usd(receipt: dict, row: dict, er_rates: dict, global_rates: dict):
    """The receipt's amount in USD, or None. A master row found in the first pass is exact;
    otherwise convert with this ER's own rate for the currency, falling back to the sheet-wide one."""
    if row.get("USD") is not None:
        return row["USD"]
    amount, cur = receipt.get("amount"), receipt.get("currency")
    if amount is None or not cur:
        return None
    if cur == "USD":
        return amount
    rate = er_rates.get(cur) or global_rates.get(cur)
    return amount * rate if rate else None


def safe_name(text: str) -> str:
    """Make a sheet value usable as a Windows file name."""
    return re.sub(r'[\\/:*?"<>|\r\n\t]', "-", text).strip(" .")


def extract_cached(path: Path, cache_dir: Path) -> dict:
    """extract_receipt with an on-disk cache keyed by file content, so a re-run
    (or a crash halfway) doesn't pay for the same receipt twice."""
    key = hashlib.sha256(path.read_bytes()).hexdigest()[:24]
    cache = cache_dir / f"{key}.json"
    if cache.exists():
        return json.loads(cache.read_text(encoding="utf-8"))
    result = extract_receipt(path.read_bytes())
    cache.write_text(json.dumps(result), encoding="utf-8")
    return result


def unique_path(folder: Path, name: str, suffix: str) -> Path:
    target = folder / f"{name}{suffix}"
    n = 2
    while target.exists():
        target = folder / f"{name}_{n}{suffix}"
        n += 1
    return target


def _rivals(label: str, rows: list[dict]) -> str:
    """The sheet rows that scored the same as the chosen one, so a tie note says what to compare."""
    def one(r):
        bits = [str(r.get("item") or ""), str(r["date"]) if r.get("date") else "",
                f"USD {r['usd']:,.2f}" if r.get("usd") is not None else ""]
        return f"{label} row {r['row']} ({', '.join(b for b in bits if b)})"
    return "; ".join(one(r) for r in rows[:3]) + ("; ..." if len(rows) > 3 else "")


def receipt_files(er_dir: Path) -> list[Path]:
    return sorted(p for p in er_dir.iterdir() if p.is_file() and p.suffix.lower() in RECEIPT_SUFFIXES)


def process_er(er_dir: Path, master_rows: list[dict] | None, annex_rows: list[dict] | None,
               out_dir: Path, cache_dir: Path, workers: int, on_read=None, rates: dict | None = None):
    """Returns (report rows, unclaimed annexure rows) for one ER folder.
    on_read, if given, is called (from a worker thread) after each receipt is read."""
    files = receipt_files(er_dir)
    rows = [{"ER": er_dir.name.upper(), "File": f.name, "Status": "", "New name": "", "Note": ""} for f in files]

    def read(path: Path):
        try:
            return extract_cached(path, cache_dir), None
        except Exception as exc:  # one bad file must not stop the run
            return None, explain_error(exc)
        finally:
            if on_read:
                on_read()

    with ThreadPoolExecutor(max_workers=max(workers, 1)) as pool:
        extracted = list(pool.map(read, files))

    receipts = []
    for row, (data, err) in zip(rows, extracted):
        if data is None:
            row["Status"], row["Note"] = "Review", f"Could not read the receipt: {err}"
            receipts.append({})
            continue
        data = dict(data, date_obj=to_date(data.get("date")))
        row.update({"Type": data["type"], "Amount": data["amount"], "Currency": data["currency"],
                    "Date": data["date"], "Confidence": data["confidence"]})
        receipts.append(data)

    if not master_rows:
        for row in rows:
            row["Status"] = row["Status"] or "Review"
            row["Note"] = row["Note"] or "ER not found in the master DATA sheet"
        return rows, []
    if not annex_rows:
        for row in rows:
            row["Status"] = row["Status"] or "Review"
            row["Note"] = row["Note"] or "ER not found in the annexure sheet"
        return rows, []

    readable = [i for i, r in enumerate(receipts) if r]
    to_master = match_receipts_to_master([receipts[i] for i in readable], master_rows)
    # Master rows that got a receipt, then pair those with annexure rows.
    claimed = {k: v for k, v in to_master.items() if v[0]["usd"] is not None}
    keys = sorted(claimed)
    to_annex = match_master_to_annexure([claimed[k][0] for k in keys], annex_rows)
    annex_for = {keys[i]: v for i, v in to_annex.items()}

    used_annex = set()
    for k, idx in enumerate(readable):
        row = rows[idx]
        if k not in to_master:
            row["Status"], row["Note"] = "Review", "No matching row in the master DATA sheet"
            continue
        m, m_score, m_tie, m_amount_only = to_master[k]
        # A master row repeated word for word (the dump can list an ER twice) leads to the same
        # annexure row whichever copy is picked: it is noted as a duplicate, not treated as a tie.
        same = lambda r: (r["usd"], r["date"], r["item"]) == (m["usd"], m["date"], m["item"])
        dupes = [r for r in m_tie if same(r)]
        m_tie = [r for r in m_tie if not same(r)]
        row.update({"Master row": m["row"], "Master item": m["item"], "Master date": m["date"], "USD": m["usd"]})
        if m["usd"] is None:
            row["Status"], row["Note"] = "Review", "Master row has no USD value"
            continue
        if k not in annex_for:
            row["Status"], row["Note"] = "Review", "No matching row in the annexure sheet"
            continue
        a, a_score, a_tie = annex_for[k]
        used_annex.add(a["row"])
        row.update({"Annexure row": a["row"], "Match score": round(min(m_score, a_score), 2)})
        row["Status"], row["New name"] = STATUS_RENAMED, safe_name(a["text"])
        notes = []
        if dupes:
            rows_txt = ", ".join(str(r["row"]) for r in [m] + dupes)
            notes.append(f"Duplicate rows in the master sheet (rows {rows_txt} are identical)")
        if m_amount_only:
            notes.append("Matched on the amount only (the date on the receipt differs from the sheet)")
        if m_tie or a_tie:
            parts = ([_rivals("master", m_tie)] if m_tie else []) + ([_rivals("annexure", a_tie)] if a_tie else [])
            notes.append("Tie: scored the same as " + "; ".join(parts) + ". Check this one")
        row["Note"] = ". ".join(notes)

    # Direct pass: when the USD amount is already known without the master (a receipt in USD, or one
    # whose master row was found but whose date didn't line up), check it against the annexure on
    # the receipt's own amount and date. The same strict rules apply, so only a clear match is renamed.
    free = [a for a in annex_rows if a["row"] not in used_annex]
    direct = []
    for idx in readable:
        r = receipts[idx]
        if is_renamed(rows[idx]) or r.get("amount") is None:
            continue
        usd = r["amount"] if r.get("currency") == "USD" else rows[idx].get("USD")
        if usd is not None:
            direct.append((idx, {"usd": usd, "date": r.get("date_obj"), "item": r.get("type")}))
    if direct and free:
        for k, (a, score, tie) in match_master_to_annexure([d for _, d in direct], free).items():
            row = rows[direct[k][0]]
            used_annex.add(a["row"])
            row.update({"Annexure row": a["row"], "Match score": round(score, 2),
                        "Status": STATUS_RENAMED, "New name": safe_name(a["text"])})
            row["Note"] = ("Matched straight to the annexure on the USD amount and the receipt date"
                           + (f". Tie: scored the same as {_rivals('annexure', tie)}" if tie else ""))

    # Second pass: each annexure name goes to one receipt, so with the sure matches taken out
    # the receipts and names that are left can be paired on weaker links.
    leftover = [idx for idx in readable if not is_renamed(rows[idx])]
    free = [a for a in annex_rows if a["row"] not in used_annex]
    if leftover and free:
        er_rates = currency_rates(master_rows)
        est = [estimate_usd(receipts[idx], rows[idx], er_rates, rates or {}) for idx in leftover]
        for k, (a, score, tie, why) in match_leftovers([receipts[i] for i in leftover], est, free).items():
            row = rows[leftover[k]]
            used_annex.add(a["row"])
            r = receipts[leftover[k]]
            where = ", ".join(str(x) for x in (a["item"], a["date"], f"${a['usd']:,.2f}" if a["usd"] is not None else "") if x)
            # Why the sure pass could not rename it: the first pass left its reason in the note.
            usd_receipt = r.get("currency") == "USD"
            if row.get("Master row") is None:
                block = "no row in the master dump matched this receipt" + ("" if usd_receipt else ", so its USD value could not be confirmed there")
            elif row.get("USD") is None:
                block = f"its master row {row['Master row']} has no USD value"
            else:
                block = (f"its master row {row['Master row']} (USD {row['USD']:,.2f}, {row.get('Master date') or 'no date'}) "
                         "did not line up with any free annexure row")
            exact = (est[k] is not None and a["usd"] is not None and abs(est[k] - a["usd"]) / max(abs(a["usd"]), 1.0) <= 0.01
                     and r.get("date_obj") and r.get("date_obj") == a["date"] and type_score(r.get("type"), a["item"]) >= 0.9)
            estimated = row.get("USD") is None and r.get("currency") != "USD"
            head = (f"Annexure row {a['row']} ({where}) agrees on amount{' (estimated)' if estimated else ''}, date and type, but "
                    if exact else f"This might match annexure row {a['row']} ({where}), but ")
            row.update({"Annexure row": a["row"], "Match score": round(score, 2),
                        "Status": STATUS_SUGGESTED, "Suggested name": safe_name(a["text"])})
            if est[k] is None or a["usd"] is None:
                amount = "the receipt's amount could not be converted to USD to compare"
            else:
                diff = est[k] - a["usd"]
                amount = (f"receipt {est[k]:,.2f} USD{' (estimated)' if estimated else ''} vs annexure ${a['usd']:,.2f}"
                          + (", the same" if abs(diff) < 0.005 else
                             f", {abs(diff):,.2f} {'less' if diff < 0 else 'more'} ({abs(diff) / max(abs(a['usd']), 1.0):.0%})"))
            if a.get("comment"):
                amount += f". Annexure comment: {a['comment']}"
            row["Note"] = ("Not renamed, suggestion only. " + head + block + ". Amount: " + amount + ". Why this row: "
                           + ("; ".join(why) if why else "weak links only")
                           + (f". Tie: scored the same as {_rivals('annexure', tie)}" if tie else ""))

    for row, f in zip(rows, files):
        if is_renamed(row):
            dest_dir = out_dir / er_dir.name.upper()
            dest_dir.mkdir(parents=True, exist_ok=True)
            target = unique_path(dest_dir, row["New name"], f.suffix.lower())
            row["New name"] = target.name
        else:
            dest_dir = out_dir / er_dir.name.upper() / REVIEW_DIR
            dest_dir.mkdir(parents=True, exist_ok=True)
            target = dest_dir / f.name
        shutil.copy2(f, target)

    unclaimed = [a for a in annex_rows if a["row"] not in used_annex]
    return rows, unclaimed


def write_report(path: Path, receipt_rows: list[dict], missing: list[dict]) -> None:
    cols = ["ER", "File", "Status", "New name", "Suggested name", "Note", "Type", "Amount", "Currency", "Date", "Confidence",
            "Master row", "Master item", "Master date", "USD", "Annexure row", "Match score"]
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Receipts"
    ws.append(cols)
    for r in receipt_rows:
        ws.append([r.get(c) for c in cols])
    # Everything still unmatched, laid out for finding it in the sheet by hand.
    ws3 = wb.create_sheet("Needs review", 1)
    ws3.append(["ER", "Amount", "Currency", "Date", "Type", "Receipt file", "Suggested name", "Reason"])
    for r in receipt_rows:
        if not is_renamed(r):
            ws3.append([r.get("ER"), r.get("Amount"), r.get("Currency"), r.get("Date"), r.get("Type"),
                        r.get("File"), r.get("Suggested name"), r.get("Note")])
    ws2 = wb.create_sheet("No receipt found")
    ws2.append(["ER", "Annexure row", "Expense item", "Expense date", "USD", "Text"])
    for a in missing:
        ws2.append([a["er"], a["row"], a["item"], a["date"], a["usd"], a["text"]])
    for sheet in (ws, ws2, ws3):
        for col in sheet.columns:
            sheet.column_dimensions[col[0].column_letter].width = min(
                max(len(str(c.value)) if c.value is not None else 0 for c in col) + 2, 48)
    wb.save(path)


def run(po_dir: Path, master_path, annexure_path, out_root: Path, workers: int = 4,
        cache_dir: Path | None = None, progress=None, log=print, extra_rows: list[dict] | None = None) -> dict:
    """Process one PO folder. progress(done, total) is called as receipts are read.
    Returns {"po", "sheet", "rows", "missing", "out_dir", "report"}."""
    po_dir = Path(po_dir).resolve()
    m = re.search(r"\d{10}", po_dir.name)
    po = m.group(0) if m else po_dir.name
    er_dirs = sorted(d for d in po_dir.iterdir() if d.is_dir() and re.match(r"(?i)^ER\d+", d.name))
    if not er_dirs:
        raise ValueError(f"No ER folders (like ER27003155204567) found in {po_dir.name}")

    log(f"PO {po}: {len(er_dirs)} ER folder(s)")
    master = load_master(str(master_path))
    sheet_name, annexure = load_annexure(str(annexure_path), po)
    log(f"Annexure sheet for this PO: {sheet_name}")

    out_dir = Path(out_root).resolve() / po
    cache_dir = Path(cache_dir) if cache_dir else Path(out_root).resolve() / ".extract_cache"
    cache_dir.mkdir(parents=True, exist_ok=True)

    total = sum(len(receipt_files(d)) for d in er_dirs)
    lock = threading.Lock()
    done = [0]

    def on_read():
        with lock:
            done[0] += 1
            if progress:
                progress(done[0], total)

    if progress:
        progress(0, total)
    rates = currency_rates(m for rows in master.values() for m in rows)
    all_rows, all_missing = [], []
    for er_dir in er_dirs:
        er = er_dir.name.upper()
        rows, missing = process_er(er_dir, master.get(er), annexure.get(er), out_dir, cache_dir, workers, on_read, rates)
        all_rows += rows
        all_missing += missing
        log(f"  {er}: {sum(1 for r in rows if is_renamed(r))}/{len(rows)} renamed")

    all_rows += extra_rows or []   # files the caller could not process (for example too large)
    out_dir.mkdir(parents=True, exist_ok=True)
    report = out_dir / "report.xlsx"
    write_report(report, all_rows, all_missing)
    return {"po": po, "sheet": sheet_name, "rows": all_rows, "missing": all_missing,
            "out_dir": out_dir, "report": report}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("po_folder", type=Path, help="folder named by PO number, containing ER subfolders")
    ap.add_argument("--master", required=True, help="master dump workbook (DATA sheet)")
    ap.add_argument("--annexure", required=True, help="Travel Annexure workbook")
    ap.add_argument("--out", type=Path, required=True, help="output folder (renamed copies and report)")
    ap.add_argument("--workers", type=int, default=4, help="receipts read in parallel (default 4)")
    args = ap.parse_args()

    try:
        result = run(args.po_folder, args.master, args.annexure, args.out, args.workers)
    except ValueError as exc:
        print(exc, file=sys.stderr)
        return 1
    rows = result["rows"]
    renamed = sum(1 for r in rows if is_renamed(r))
    print(f"Done: {renamed}/{len(rows)} receipts renamed, {len(rows) - renamed} need review.")
    print(f"Report: {result['report']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
