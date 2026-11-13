"""
Readers for the two Excel files the renaming flow depends on.

  - Master dump (sheet "DATA"): one row per expense line, with the USD amount.
  - Travel Annexure: an "Overview" sheet maps a PO number to a project-code sheet;
    that sheet holds the rows with the "Text" value a receipt must be renamed to.

Headers are found by name (case and spacing ignored) so extra title rows or
column shifts in the workbooks don't matter.
"""

import re
from datetime import date, datetime

import openpyxl


def _norm(value) -> str:
    return re.sub(r"\s+", " ", str(value)).strip().lower() if value is not None else ""


def to_date(value):
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if isinstance(value, str):
        for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d", "%d-%m-%Y", "%d/%m/%Y"):
            try:
                return datetime.strptime(value.strip(), fmt).date()
            except ValueError:
                pass
    return None


def to_float(value):
    if value is None or value == "":
        return None
    try:
        return float(str(value).replace(",", ""))
    except ValueError:
        return None


def _open(path: str, label: str):
    """Open a workbook read-only, with a readable error if it isn't a real Excel file."""
    try:
        return openpyxl.load_workbook(path, read_only=True, data_only=True)
    except Exception as exc:
        raise ValueError(f"The {label} file is not a valid Excel workbook (.xlsx). Choose it again. ({exc})") from exc


def _sheet(wb, name: str):
    for ws in wb.worksheets:
        if ws.title.strip().lower() == name.strip().lower():
            return ws
    raise ValueError(f"Sheet {name!r} not found. Sheets in the file: {wb.sheetnames}")


def _find_header(ws, required: list[str], scan: int = 40):
    """Return (header row number, {normalised header: column index})."""
    for n, row in enumerate(ws.iter_rows(min_row=1, max_row=scan, values_only=True), 1):
        cols: dict[str, int] = {}
        for i, v in enumerate(row):
            key = _norm(v)
            if key and key not in cols:
                cols[key] = i
        if all(r in cols for r in required):
            return n, cols
    raise ValueError(f"Sheet {ws.title!r}: no header row containing {required} in the first {scan} rows")


def _rows(ws, header_row: int, cols: dict[str, int]):
    """Yield (excel row number, getter) for every row below the header."""
    for n, row in enumerate(ws.iter_rows(min_row=header_row + 1, values_only=True), header_row + 1):
        yield n, (lambda key, row=row: row[cols[key]] if key in cols and cols[key] < len(row) else None)


def load_master(path: str, sheet: str = "DATA") -> dict[str, list[dict]]:
    """Master dump rows grouped by ER tracking number."""
    wb = _open(path, "master dump")
    try:
        ws = _sheet(wb, sheet)
        hdr, cols = _find_header(ws, ["er tracking number", "expense item", "expense receipt date",
                                      "claimed amount", "usd"])
        by_er: dict[str, list[dict]] = {}
        for n, get in _rows(ws, hdr, cols):
            er = str(get("er tracking number") or "").strip().upper()
            if not er.startswith("ER"):
                continue
            by_er.setdefault(er, []).append({
                "row": n,
                "er": er,
                "item": str(get("expense item") or "").strip(),
                "date": to_date(get("expense receipt date")),
                "claimed": to_float(get("claimed amount")),
                "item_amount": to_float(get("expense item amount")),
                "converted": to_float(get("converted expense item amount")),
                "currency": get("expense item currency"),
                "usd": to_float(get("usd")),
            })
        return by_er
    finally:
        wb.close()


def load_annexure(path: str, po: str) -> tuple[str, dict[str, list[dict]]]:
    """(project-code sheet name, its rows grouped by ER) for one PO number."""
    wb = _open(path, "travel annexure")
    try:
        ov = _sheet(wb, "Overview")
        hdr, cols = _find_header(ov, ["po no", "project code"])
        sheet_name = None
        for _, get in _rows(ov, hdr, cols):
            v = get("po no")
            v = int(v) if isinstance(v, float) and v.is_integer() else v
            if str(v).strip() == po:
                sheet_name = str(get("project code")).strip()
                break
        if not sheet_name:
            raise ValueError(f"PO {po} not found in the Overview sheet")

        ws = _sheet(wb, sheet_name)
        hdr, cols = _find_header(ws, ["er", "text", "expense item", "expense date", "amt in usd"])
        by_er: dict[str, list[dict]] = {}
        for n, get in _rows(ws, hdr, cols):
            er = str(get("er") or "").strip().upper()
            text = get("text")
            if not er.startswith("ER") or not text:
                continue
            by_er.setdefault(er, []).append({
                "row": n,
                "er": er,
                "item": str(get("expense item") or "").strip(),
                "date": to_date(get("expense date")),
                "from": to_date(get("from date")),
                "to": to_date(get("to date")),
                "usd": to_float(get("amt in usd")),
                "text": str(text).strip(),
                "receipt_no": get("receipt #"),
                "comment": str(get("comments") or "").strip(),
            })
        return ws.title, by_er
    finally:
        wb.close()
