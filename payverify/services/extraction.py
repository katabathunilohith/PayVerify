"""Read a UPI payment screenshot with Meta's Muse Spark model (Meta Model API,
https://dev.meta.ai) and return the payment details as structured data.

Uses the Standard tier, where Meta doesn't train on your data. Avoid the
"-contributor" models for customer payment screenshots: Meta trains on those.
"""

import base64
import json
import logging
import re
import time
from dataclasses import dataclass, field

import requests
from flask import current_app

log = logging.getLogger(__name__)

API_URL = "https://api.meta.ai/v1/chat/completions"
RETRY_STATUSES = {429, 500, 502, 503, 504}

PAYMENT_APPS = [
    "Google Pay", "PhonePe", "Paytm", "BHIM", "Amazon Pay", "WhatsApp Pay",
    "CRED", "MobiKwik", "Bank app", "Other", "Unknown",
]
TRANSACTION_STATUSES = ["success", "pending", "failed", "unknown"]


def _nullable(schema):
    return {"anyOf": [schema, {"type": "null"}]}


SCHEMA = {
    "type": "object",
    "properties": {
        "is_payment_screenshot": {"type": "boolean"},
        "transaction_status": {"type": "string", "enum": TRANSACTION_STATUSES},
        "amount": _nullable({"type": "number"}),
        "utr": _nullable({"type": "string"}),
        "app_transaction_id": _nullable({"type": "string"}),
        "paid_at": _nullable({"type": "string"}),
        "payee_name": _nullable({"type": "string"}),
        "payee_upi_id": _nullable({"type": "string"}),
        "payer_name": _nullable({"type": "string"}),
        "payer_upi_id": _nullable({"type": "string"}),
        "payment_app": {"type": "string", "enum": PAYMENT_APPS},
        "looks_edited": {"type": "boolean"},
        "warnings": {"type": "array", "items": {"type": "string"}},
    },
    "required": [
        "is_payment_screenshot", "transaction_status", "amount", "utr", "app_transaction_id",
        "paid_at", "payee_name", "payee_upi_id", "payer_name", "payer_upi_id", "payment_app",
        "looks_edited", "warnings",
    ],
}

SYSTEM_PROMPT = """You read screenshots of Indian UPI payment receipts (Google Pay, PhonePe, Paytm, BHIM, bank apps and similar) for a small business that records the payments its customers send it.

Copy exactly what is visible. Never guess or fill in a value that is not on the screen: use null when a field is missing or not legible.

- amount: the amount paid, in rupees, as a number (for example 1250.5). Ignore balances, cashback and fees.
- utr: the 12-digit UPI reference number. Apps label it "UPI transaction ID" (Google Pay), "UTR" (PhonePe), "UPI Ref No" (Paytm), "RRN" or "Reference number". Return only the digits. App-specific IDs such as PhonePe's "T..." transaction ID or Google Pay's "Google transaction ID" go in app_transaction_id, never in utr.
- paid_at: the payment date and time printed on the receipt, formatted YYYY-MM-DDTHH:MM in 24-hour local time. If the receipt shows no year, use the most recent such date that is not after today.
- payee_name and payee_upi_id: who received the money ("To", "Paid to"). payer_name and payer_upi_id: who sent it ("From", "Debited from").
- transaction_status: "success" only when the receipt clearly says the payment succeeded, completed or was paid; otherwise "pending", "failed" or "unknown".
- payment_app: the app that produced the screenshot, judged from its layout and branding.
- looks_edited: true only when there are clear signs the screenshot was edited or faked, such as digits in a different font, size or colour from the text around them, misaligned or blurred numbers, or details that contradict each other. Otherwise false.
- warnings: short notes on anything a careful shop owner should know, such as a status other than success, a cropped receipt, or the signs of editing you saw. Use an empty list when nothing stands out.
- is_payment_screenshot: false when the image is not a payment receipt at all.

Reply with the JSON object only."""


@dataclass
class Extraction:
    ok: bool
    error: str = ""
    data: dict = field(default_factory=dict)


def _post(payload, api_key):
    """POST to the Meta Model API, retrying once on busy/server errors.

    Uploads wait for this call, so the worst case stays under gunicorn's 120 s timeout.
    """
    for attempt in range(2):
        try:
            response = requests.post(
                API_URL,
                headers={"Authorization": f"Bearer {api_key}"},
                json=payload,
                timeout=50,
            )
        except requests.RequestException as exc:
            log.warning("Meta Model API unreachable: %s", exc.__class__.__name__)
            if attempt == 0:
                time.sleep(2)
                continue
            return None
        if response.status_code in RETRY_STATUSES and attempt == 0:
            time.sleep(2)
            continue
        return response
    return None


def _message_text(message):
    content = message.get("content")
    if isinstance(content, list):  # some servers return content parts
        content = "".join(part.get("text", "") for part in content if isinstance(part, dict))
    return content or ""


def parse_json_object(text):
    """Accept plain JSON, JSON inside ``` fences, or JSON surrounded by prose."""
    text = (text or "").strip()
    fenced = re.match(r"^```(?:json)?\s*(.*?)\s*```$", text, re.S)
    if fenced:
        text = fenced.group(1)
    try:
        data = json.loads(text)
    except ValueError:
        match = re.search(r"\{.*\}", text, re.S)
        if not match:
            return None
        try:
            data = json.loads(match.group(0))
        except ValueError:
            return None
    return data if isinstance(data, dict) else None


def extract_payment_details(image_bytes, media_type, today, tz_name):
    """Send one screenshot to Meta's model. Never raises; failures come back as ok=False."""
    config = current_app.config
    api_key = config.get("META_MODEL_API_KEY")
    if not api_key:
        return Extraction(False, "Automatic reading isn't set up. Add META_MODEL_API_KEY on the server (see README).")

    image_b64 = base64.standard_b64encode(image_bytes).decode("ascii")
    payload = {
        "model": config["AI_MODEL"],
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": [
                {"type": "text", "text": f"Today is {today:%Y-%m-%d} (time zone {tz_name}). Read this payment screenshot."},
                {"type": "image_url", "image_url": {"url": f"data:{media_type};base64,{image_b64}"}},
            ]},
        ],
        "response_format": {
            "type": "json_schema",
            "json_schema": {"name": "upi_payment_receipt", "schema": SCHEMA},
        },
        "reasoning_effort": config["AI_EFFORT"],
        "max_completion_tokens": 8000,
    }
    response = _post(payload, api_key)
    if response is None:
        return Extraction(False, "Couldn't reach Meta's AI service. Check the server's internet connection, then use “Read again”.")
    status = response.status_code
    if status in (401, 403):
        return Extraction(False, "Meta rejected the API key. Check META_MODEL_API_KEY (create one at dev.meta.ai).")
    if status == 429:
        return Extraction(False, "Meta's AI service is busy (rate limited). Use “Read again” in a minute.")
    if status >= 400:
        log.warning("Meta Model API error %s: %s", status, response.text[:500])
        return Extraction(False, f"Meta's AI service returned an error ({status}). Try “Read again” later.")

    try:
        choice = response.json()["choices"][0]
        message = choice.get("message") or {}
    except (ValueError, KeyError, IndexError, TypeError):
        return Extraction(False, "Meta's AI service sent an unexpected reply. Try “Read again”.")
    finish = choice.get("finish_reason")
    if finish == "refusal" or message.get("refusal"):
        return Extraction(False, "The AI declined to read this image.")
    if finish == "length":
        return Extraction(False, "The AI's answer was cut off. Try “Read again”.")
    data = parse_json_object(_message_text(message))
    if data is None:
        return Extraction(False, "The AI returned an unreadable answer. Try “Read again”.")
    return Extraction(True, data=data)


def check_api_key():
    """Make one tiny request to confirm the key works. Returns (ok, message)."""
    config = current_app.config
    api_key = config.get("META_MODEL_API_KEY")
    if not api_key:
        return False, "not set. Add META_MODEL_API_KEY to the .env file (create a key at dev.meta.ai)."
    response = _post({
        "model": config["AI_MODEL"],
        "messages": [{"role": "user", "content": "Reply with the word OK."}],
        "reasoning_effort": "minimal",
        "max_completion_tokens": 200,
    }, api_key)
    if response is None:
        return False, "couldn't reach api.meta.ai. Check the internet connection."
    if response.status_code in (401, 403):
        return False, "Meta rejected the key. Check that it was copied completely."
    if response.status_code >= 400:
        return False, f"Meta returned an error (HTTP {response.status_code}): {response.text[:200]}"
    return True, f"works (model {config['AI_MODEL']})."
