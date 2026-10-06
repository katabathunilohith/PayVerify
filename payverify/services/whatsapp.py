"""WhatsApp Business Cloud API: verify webhooks, download images, send replies."""

import hashlib
import hmac
from urllib.parse import urlparse

import requests
from flask import current_app

GRAPH_URL = "https://graph.facebook.com"
MEDIA_HOSTS = ("fbsbx.com", "facebook.com", "fbcdn.net", "whatsapp.net")
MAX_MEDIA_BYTES = 15 * 1024 * 1024


# Plain-language fixes for the errors people hit while setting up.
ERROR_HINTS = {
    190: "The access token has expired or is wrong. Create a permanent system-user token.",
    100: "Check the phone number ID, and that the token belongs to the same Meta app.",
    131030: "On Meta's test number you can only message numbers added under API Setup → To.",
    131047: "More than 24 hours have passed since this person last messaged you, so WhatsApp only allows template messages.",
    131026: "WhatsApp couldn't deliver it. Is the number on WhatsApp, with the country code?",
    132001: "The message template doesn't exist for this number.",
    133010: "This phone number isn't registered with the Cloud API yet. Finish adding it in WhatsApp Manager.",
}


class WhatsAppError(Exception):
    def __init__(self, message, code=None):
        hint = ERROR_HINTS.get(code)
        super().__init__(f"{message} {hint}" if hint else message)
        self.code = code


def verify_signature(app_secret, raw_body, header):
    """Meta signs every webhook with the app secret (X-Hub-Signature-256)."""
    if not app_secret or not header or not header.startswith("sha256="):
        return False
    expected = hmac.new(app_secret.encode(), raw_body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, header[len("sha256="):])


def iter_incoming_images(payload, phone_number_id):
    """Yield the image messages in a webhook payload sent to our number.

    Screenshots sent "as a document" are included when they are images.
    """
    for entry in payload.get("entry") or []:
        for change in entry.get("changes") or []:
            value = change.get("value") or {}
            metadata = value.get("metadata") or {}
            if phone_number_id and str(metadata.get("phone_number_id")) != str(phone_number_id):
                continue
            names = {
                contact.get("wa_id"): (contact.get("profile") or {}).get("name", "")
                for contact in value.get("contacts") or []
            }
            for message in value.get("messages") or []:
                kind = message.get("type")
                media = message.get(kind) if kind in ("image", "document") else None
                if not media or not media.get("id"):
                    continue
                if kind == "document" and not str(media.get("mime_type", "")).startswith("image/"):
                    continue
                sender = message.get("from", "")
                yield {
                    "message_id": message.get("id"),
                    "from": sender,
                    "name": names.get(sender, ""),
                    "media_id": media["id"],
                    "caption": media.get("caption", ""),
                }


def _graph(path):
    return f"{GRAPH_URL}/{current_app.config['WHATSAPP_GRAPH_VERSION']}/{path}"


def _headers(user):
    token = user.wa_access_token
    if not token:
        raise WhatsAppError("WhatsApp access token isn't saved in Settings.")
    return {"Authorization": f"Bearer {token}"}


def _api_error(response):
    try:
        error = response.json().get("error", {})
    except ValueError:
        return WhatsAppError(f"HTTP {response.status_code}")
    code = error.get("code")
    details = (error.get("error_data") or {}).get("details") or error.get("message") or "Unknown error"
    return WhatsAppError(f"{details} (code {code or response.status_code}).", code)


def download_media(user, media_id):
    """Return (bytes, mime_type) for a WhatsApp media id."""
    headers = _headers(user)
    try:
        meta = requests.get(_graph(media_id), headers=headers, timeout=20)
        if meta.status_code != 200:
            raise _api_error(meta)
        info = meta.json()
        url = info.get("url", "")
        host = urlparse(url).hostname or ""
        if not url.startswith("https://") or not any(host == h or host.endswith("." + h) for h in MEDIA_HOSTS):
            raise WhatsAppError("Unexpected media URL from WhatsApp.")
        with requests.get(url, headers=headers, timeout=60, stream=True) as resp:
            if resp.status_code != 200:
                raise WhatsAppError(f"Media download failed (HTTP {resp.status_code}).")
            chunks, size = [], 0
            for chunk in resp.iter_content(64 * 1024):
                size += len(chunk)
                if size > MAX_MEDIA_BYTES:
                    raise WhatsAppError("The image is too large.")
                chunks.append(chunk)
        return b"".join(chunks), info.get("mime_type", "")
    except requests.RequestException as exc:
        raise WhatsAppError(f"Couldn't reach WhatsApp: {exc.__class__.__name__}") from exc


def _send(user, payload):
    try:
        resp = requests.post(
            _graph(f"{user.wa_phone_number_id}/messages"), headers=_headers(user), json=payload, timeout=20
        )
    except requests.RequestException as exc:
        raise WhatsAppError(f"Couldn't reach WhatsApp: {exc.__class__.__name__}") from exc
    if resp.status_code >= 300:
        raise _api_error(resp)


def send_text(user, to, body, reply_to=None):
    payload = {
        "messaging_product": "whatsapp",
        "recipient_type": "individual",
        "to": to,
        "type": "text",
        "text": {"preview_url": False, "body": body[:4096]},
    }
    if reply_to:
        payload["context"] = {"message_id": reply_to}
    _send(user, payload)


def send_test_template(user, to):
    """Meta's ready-made "hello_world" template. Unlike a normal text, a template
    can start a conversation, so this works before the person has messaged you."""
    _send(user, {
        "messaging_product": "whatsapp",
        "to": to,
        "type": "template",
        "template": {"name": "hello_world", "language": {"code": "en_US"}},
    })


def check_connection(user):
    """Return the business number's display name, or raise WhatsAppError."""
    if not user.wa_phone_number_id:
        raise WhatsAppError("Phone number ID isn't saved.")
    try:
        resp = requests.get(
            _graph(user.wa_phone_number_id),
            headers=_headers(user),
            params={"fields": "display_phone_number,verified_name"},
            timeout=20,
        )
    except requests.RequestException as exc:
        raise WhatsAppError(f"Couldn't reach WhatsApp: {exc.__class__.__name__}") from exc
    if resp.status_code != 200:
        raise _api_error(resp)
    data = resp.json()
    return f"{data.get('verified_name', '')} ({data.get('display_phone_number', '')})".strip()
