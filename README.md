# Receipt extractor and renamer

Reads travel receipts (PDF or image) and renames them to the Text value from the Travel Annexure.

```
pip install -r requirements.txt
az login
```

## Web app

```
python app.py          # http://127.0.0.1:8001
```

- **Rename PO folder** (`/`): choose the **master dump** and **travel annexure** Excel files, then choose or drop a folder
  named by the PO number with one subfolder per ER (`4200007104/ER27003155204567/...`). Click *Rename receipts*,
  review the table, then *Download renamed files* (zip). Optional `MASTER_FILE` / `ANNEXURE_FILE` in `.env` act as saved
  defaults; a file chosen on the page always wins.
- **Single receipt** (`/single`): read the amount, date and type of one receipt.

Uploaded folders are kept in a temporary folder and deleted on *Clear* or after 24 hours.

## Command line

```
python rename_receipts.py PO_FOLDER --master "Monster 12 Oct.xlsx" --annexure "October 2026 Travel Annexure.xlsx" --out OUTPUT
```

For every receipt: read type, amount and date, find its row in the master `DATA` sheet (for USD),
find the matching row in the annexure sheet the `Overview` sheet assigns to the PO, and copy the
receipt as `<Text>.<ext>` into `OUTPUT/<PO>/<ER>/`. Unclear ones go to `NeedsReview/`. Originals are untouched.
A second pass then pairs what is left **by elimination**: each annexure name belongs to one receipt, so the receipts
that didn't match and the names nobody claimed are paired on weaker links (amount converted to USD, date or the
From/To range, type). Those are marked `Renamed (elimination)` with the reasons in the Note column.
`OUTPUT/<PO>/report.xlsx` lists every receipt, its match, and annexure rows that got no receipt.

## Files

- `extractor.py`: reads one receipt with the Azure vision model (Entra ID auth)
- `sheets.py`: reads the master and annexure workbooks
- `matcher.py`: scores and assigns receipts to sheet rows
- `rename_receipts.py`: the folder pipeline (`run()` is used by the web app)
- `app.py`: web server and upload job API
- `static/`: `index.html` (PO folder page), `single.html` (single receipt), `style.css`
- `categories.txt`: expense types the extractor may return

Settings are env vars, see `.env.example`. The model deployment must accept images.
