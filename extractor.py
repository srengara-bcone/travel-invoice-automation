"""
Receipt field extraction with an Azure-hosted vision model.

Takes the bytes of one receipt (image or PDF) and returns the amount, date and
expense type. Auth is Entra ID (`az login`), no API key.
"""

import base64
import io
import json
import os
import re
from datetime import date
from pathlib import Path
from typing import Optional

AZURE_ENDPOINT_DEFAULT = "https://ai-adoption-coe.services.ai.azure.com/openai/v1/"
# Must accept images: DeepSeek-V4-Pro is text-only and rejects them.
AZURE_DEPLOYMENT_DEFAULT = "gpt-5.4"

# Expense categories come from categories.txt (one per line) so they can be
# changed without touching code. "Other" is always allowed as a fallback.
CATEGORIES_FILE = Path(__file__).with_name("categories.txt")
FALLBACK_TYPE = "Other"

# Shrink big photos first: keeps uploads fast and under API size limits.
MAX_SIDE = 1568
MAX_BYTES = 4 * 1024 * 1024
SUPPORTED_MEDIA = {"image/jpeg", "image/png", "image/gif", "image/webp"}

# PDFs are rendered to images; only the first pages matter for a receipt.
PDF_MAX_PAGES = 3
PDF_DPI = 150
PDF_TEXT_CHARS = 4000

_PROMPT = (
    "Read this travel-expense receipt. Use the final total actually paid. Day and month "
    "order in dates is ambiguous on some receipts: use the country or currency to decide, "
    "and lower the confidence if still unsure. Today's date is {today}; a date in the "
    "future means you misread it.\n\n"
    "Pick the expense type from this list only, using the closest match:\n{types}\n"
    "Use \"{fallback}\" if none fits. Choose by what was purchased. Some categories differ "
    "only by the traveller's seniority or by a meal slot, which a receipt may not show: "
    "use the meal slot (breakfast, lunch, dinner) when the time or items make it clear; "
    "otherwise prefer the plain category without a seniority restriction. Use the "
    "Cab/Shuttle/Bus/Train category only when the ride is a bus or train.\n\n"
    "Reply with ONLY a JSON object (no markdown) with these keys:\n{schema}"
)

_SCHEMA = {
    "amount": "Final total paid, tax and tip included. Not a subtotal. Null if no total is legible.",
    "currency": "ISO 4217 code such as USD, EUR, INR, GBP. Infer from the symbol or country "
                "if no code is printed. Null if unknown.",
    "date": "Transaction date as YYYY-MM-DD. Null if no date is legible.",
    "type": "The expense type, exactly as written in the list.",
    "confidence": "high, medium or low. Low if the document is blurry, cropped or the total is ambiguous.",
    "notes": "One short sentence if anything was unclear, else null.",
}

_CLIENT = None


def load_categories() -> list[str]:
    """Categories from categories.txt, read each call so edits apply immediately."""
    try:
        lines = CATEGORIES_FILE.read_text(encoding="utf-8").splitlines()
    except OSError:
        lines = []
    cats = [ln.strip() for ln in lines if ln.strip() and not ln.lstrip().startswith("#")]
    if FALLBACK_TYPE not in cats:
        cats.append(FALLBACK_TYPE)
    return cats


def _http_client():
    """httpx client that trusts the corporate CA on networks with SSL inspection:
    CLAUDE_CA_BUNDLE / SSL_CERT_FILE if set, else the Windows certificate store."""
    import httpx

    ca_bundle = (os.getenv("CLAUDE_CA_BUNDLE") or os.getenv("REQUESTS_CA_BUNDLE")
                 or os.getenv("SSL_CERT_FILE"))
    if ca_bundle and os.path.isfile(ca_bundle):
        return httpx.Client(verify=ca_bundle)
    try:
        import ssl
        import truststore
        return httpx.Client(verify=truststore.SSLContext(ssl.PROTOCOL_TLS_CLIENT))
    except ImportError:
        return None


def _get_client():
    """OpenAI client for the Azure endpoint. The token provider is passed as a
    callable so the SDK refreshes the bearer token instead of using one that
    expires after an hour."""
    global _CLIENT
    if _CLIENT is None:
        from azure.identity import DefaultAzureCredential, get_bearer_token_provider
        from openai import OpenAI

        token_provider = get_bearer_token_provider(
            DefaultAzureCredential(), "https://cognitiveservices.azure.com/.default")
        kwargs = {}
        http_client = _http_client()
        if http_client is not None:
            kwargs["http_client"] = http_client
        _CLIENT = OpenAI(
            base_url=os.getenv("AZURE_OPENAI_ENDPOINT", AZURE_ENDPOINT_DEFAULT),
            api_key=token_provider,
            timeout=float(os.getenv("AZURE_OPENAI_TIMEOUT", "90")),
            **kwargs,
        )
    return _CLIENT


def _prepare_image(raw: bytes) -> tuple[bytes, str]:
    """Return (bytes, media_type) that the API will accept."""
    from PIL import Image, ImageOps

    try:
        img = Image.open(io.BytesIO(raw))
        img.load()
    except Exception as exc:
        raise ValueError("The file is not a readable image or PDF (it may be corrupted)") from exc

    media = Image.MIME.get(img.format or "", "")
    if media in SUPPORTED_MEDIA and len(raw) <= MAX_BYTES and max(img.size) <= MAX_SIDE:
        return raw, media

    # Phone photos carry rotation in EXIF; apply it so the text is upright.
    img = ImageOps.exif_transpose(img)
    if max(img.size) > MAX_SIDE:
        img.thumbnail((MAX_SIDE, MAX_SIDE))
    if img.mode not in ("RGB", "L"):
        img = img.convert("RGB")
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=90)
    return buf.getvalue(), "image/jpeg"


def _prepare_pdf(raw: bytes) -> tuple[list[tuple[bytes, str]], str]:
    """Render the first pages of a PDF to images and pull out any embedded text.
    Digital PDFs carry exact text, which is more reliable than reading pixels."""
    import pymupdf

    try:
        doc = pymupdf.open(stream=raw, filetype="pdf")
    except Exception as exc:
        raise ValueError(f"Not a readable PDF: {exc}") from exc

    try:
        if doc.needs_pass:
            raise ValueError("The PDF is password protected")
        if doc.page_count == 0:
            raise ValueError("The PDF has no pages")
        pages, text = [], []
        for page in doc.pages(0, min(doc.page_count, PDF_MAX_PAGES)):
            png = page.get_pixmap(dpi=PDF_DPI, alpha=False).tobytes("png")
            pages.append(_prepare_image(png))
            text.append(page.get_text("text").strip())
    finally:
        doc.close()
    return pages, "\n".join(t for t in text if t)[:PDF_TEXT_CHARS]


def _parse_json(text: str) -> dict:
    """Pull the JSON object out of a model reply that may be fenced or chatty."""
    text = (text or "").strip()
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end <= start:
        raise RuntimeError(f"Model did not return JSON: {text[:200]!r}")
    return json.loads(text[start:end + 1])


def _clean(data: dict, categories: list[str]) -> dict:
    amount = data.get("amount")
    try:
        amount = round(float(amount), 2) if amount is not None else None
    except (TypeError, ValueError):
        amount = None

    currency = (data.get("currency") or "").strip().upper() or None
    if currency and not re.fullmatch(r"[A-Z]{3}", currency):
        currency = None

    day = data.get("date")
    if day and not re.fullmatch(r"\d{4}-\d{2}-\d{2}", str(day)):
        day = None

    # Accept the model's answer case-insensitively, but only if it's a real category.
    by_lower = {c.lower(): c for c in categories}
    etype = by_lower.get(str(data.get("type") or "").strip().lower(), FALLBACK_TYPE)

    confidence = data.get("confidence")
    return {
        "amount": amount,
        "currency": currency,
        "date": day,
        "type": etype,
        "confidence": confidence if confidence in ("high", "medium", "low") else "low",
        "notes": data.get("notes"),
    }


def extract_receipt(file_bytes: bytes, model: Optional[str] = None) -> dict:
    """Extract amount, date and expense type from one receipt (image or PDF)."""
    # The PDF spec allows junk before the header, within the first 1 KB.
    if b"%PDF-" in file_bytes[:1024]:
        images, pdf_text = _prepare_pdf(file_bytes)
    else:
        images, pdf_text = [_prepare_image(file_bytes)], ""

    model = model or os.getenv("AZURE_OPENAI_DEPLOYMENT", AZURE_DEPLOYMENT_DEFAULT)
    categories = load_categories()
    prompt = _PROMPT.format(
        today=date.today().isoformat(),
        types="\n".join(f"- {c}" for c in categories),
        fallback=FALLBACK_TYPE,
        schema=json.dumps(_SCHEMA, indent=2),
    )
    if pdf_text:
        prompt += f"\n\nText extracted from the PDF (may be incomplete):\n{pdf_text}"

    content = [{"type": "text", "text": prompt}]
    for raw, media in images:
        data_url = f"data:{media};base64,{base64.standard_b64encode(raw).decode('ascii')}"
        content.append({"type": "image_url", "image_url": {"url": data_url}})

    kwargs = {}
    if model.lower().startswith(("gpt-5", "o1", "o3")):
        # Reasoning models spend the output budget on hidden thinking; cap it.
        kwargs["reasoning_effort"] = os.getenv("AZURE_OPENAI_REASONING_EFFORT", "low")

    resp = _get_client().chat.completions.create(
        model=model,
        max_completion_tokens=int(os.getenv("AZURE_OPENAI_MAX_OUTPUT_TOKENS", "1500")),
        messages=[{"role": "user", "content": content}],
        **kwargs,
    )

    result = _clean(_parse_json(resp.choices[0].message.content), categories)
    result["model"] = model
    return result


def explain_error(exc: Exception) -> str:
    """A short, readable reason for a failed model call (the raw API error is long JSON)."""
    text = str(exc)
    low = text.lower()
    if "virtual network" in low or "cogsvc-vnet" in low:
        return ("The AI service refused the request because this computer is not on an allowed network. "
                "Connect to the company network or VPN and run it again.")
    if "401" in low or "unauthorized" in low or "credential" in low or "az login" in low:
        return "Sign-in to Azure failed or expired. Run `az login` and try again."
    if "429" in low or "rate limit" in low or "too many requests" in low:
        return "The AI service is busy (rate limit). Run it again in a minute, or lower RECEIPT_WORKERS in .env."
    if "timed out" in low or "timeout" in low:
        return "The AI service took too long to answer. Run it again."
    if "connection" in low:
        return "Could not connect to the AI service. Check your network or VPN and run it again."
    return text
