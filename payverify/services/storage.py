"""Screenshot files. Each account gets its own folder, files get random names,
and they are only ever served through a logged-in route that checks ownership."""

import hashlib
import io
import os
import uuid
from dataclasses import dataclass

from flask import current_app
from PIL import Image, ImageOps, UnidentifiedImageError

FORMATS = {"JPEG": ("image/jpeg", "jpg"), "PNG": ("image/png", "png"), "WEBP": ("image/webp", "webp"), "GIF": ("image/gif", "gif")}
EXTENSIONS = {mime: ext for mime, ext in FORMATS.values()}
# Meta accepts much larger images, but smaller ones upload and read faster.
MAX_AI_BYTES = 4_500_000
MAX_SIDE = 7_900


class InvalidImage(ValueError):
    pass


@dataclass
class StoredImage:
    rel_path: str
    mime: str
    sha256: str
    data: bytes


def normalize_image(raw):
    """Return (bytes, mime) as a PNG/JPEG/WebP/GIF of a sensible size for the AI."""
    try:
        with Image.open(io.BytesIO(raw)) as probe:
            probe.verify()
        img = Image.open(io.BytesIO(raw))
        img.load()
    except (UnidentifiedImageError, OSError, SyntaxError, ValueError, Image.DecompressionBombError) as exc:
        raise InvalidImage("That file isn't an image we can read. Please send a PNG or JPG screenshot.") from exc

    if img.format in FORMATS and len(raw) <= MAX_AI_BYTES and max(img.size) <= MAX_SIDE:
        return raw, FORMATS[img.format][0]

    img = ImageOps.exif_transpose(img)
    img.thumbnail((4000, 4000))
    if img.mode not in ("RGB", "L"):
        img = img.convert("RGB")
    quality = 90
    while True:
        out = io.BytesIO()
        img.save(out, "JPEG", quality=quality)
        data = out.getvalue()
        if len(data) <= MAX_AI_BYTES or quality <= 40:
            return data, "image/jpeg"
        quality -= 15


def _root():
    return os.path.abspath(current_app.config["UPLOAD_FOLDER"])


def save_screenshot(user_id, raw):
    data, mime = normalize_image(raw)
    folder = os.path.join(_root(), str(int(user_id)))
    os.makedirs(folder, exist_ok=True)
    name = f"{uuid.uuid4().hex}.{EXTENSIONS[mime]}"
    with open(os.path.join(folder, name), "wb") as fh:
        fh.write(data)
    return StoredImage(f"{int(user_id)}/{name}", mime, hashlib.sha256(data).hexdigest(), data)


def absolute_path(rel_path):
    root = _root()
    path = os.path.abspath(os.path.join(root, rel_path))
    if not path.startswith(root + os.sep):
        raise ValueError("Path outside the upload folder")
    return path


def read_screenshot(rel_path):
    with open(absolute_path(rel_path), "rb") as fh:
        return fh.read()


def delete_screenshot(rel_path):
    if not rel_path:
        return
    try:
        os.remove(absolute_path(rel_path))
    except (FileNotFoundError, ValueError):
        pass
