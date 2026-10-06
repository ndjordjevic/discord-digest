"""Download the image attachments a digest references, so they outlive Discord.

Discord CDN attachment URLs are signed and expire (~24h after export), so a
digest can't link to them. Images the summarizer picked are saved next to the
digest under `assets/<week>/`; other files (zips etc.) are only listed by name.
"""

from __future__ import annotations

import io
import urllib.error
import urllib.request
from pathlib import Path

from PIL import Image, ImageOps, UnidentifiedImageError

from .parse import is_image

MAX_IMAGES_PER_TOPIC = 4
# Phone photos are often 2-3 MB; downscale before committing them to the repo.
MAX_IMAGE_SIDE = 1200
_USER_AGENT = "discord-digest/0.1"


def index_attachments(messages: list[dict]) -> dict[str, dict]:
    """{attachment_id: {url, file_name, size_bytes, msg_id}} for the given messages."""
    out: dict[str, dict] = {}
    for m in messages:
        for a in m.get("attachments") or []:
            if a.get("id"):
                out[a["id"]] = {
                    "url": a.get("url"),
                    "file_name": a.get("fileName") or "file",
                    "size_bytes": a.get("fileSizeBytes") or 0,
                    "msg_id": m.get("id"),
                }
    return out


def _download(url: str, dest: Path) -> bool:
    if dest.exists():
        return True
    dest.parent.mkdir(parents=True, exist_ok=True)
    req = urllib.request.Request(url, headers={"User-Agent": _USER_AGENT})
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            data = resp.read()
    except (urllib.error.URLError, TimeoutError):
        return False
    dest.write_bytes(_shrink(data, dest.suffix.lower()))
    return True


def _shrink(data: bytes, suffix: str) -> bytes:
    """Downscale large stills to MAX_IMAGE_SIDE; GIFs (maybe animated) pass through."""
    if suffix == ".gif":
        return data
    try:
        img = Image.open(io.BytesIO(data))
        if max(img.size) <= MAX_IMAGE_SIDE:
            return data
        # Re-encoding drops EXIF, so bake the phone's rotation into the pixels first.
        img = ImageOps.exif_transpose(img)
        img.thumbnail((MAX_IMAGE_SIDE, MAX_IMAGE_SIDE))
        buf = io.BytesIO()
        if suffix in (".jpg", ".jpeg"):
            img.convert("RGB").save(buf, "JPEG", quality=82, optimize=True)
        else:
            img.save(buf, img.format or "PNG", optimize=True)
        return buf.getvalue()
    except (UnidentifiedImageError, OSError):
        return data


def fetch_images(attachment_ids: list[str], attachments: dict[str, dict], digest_dir: Path, week: str) -> None:
    """Download the picked image attachments; sets `local` (path relative to digest_dir) on success."""
    for att_id in attachment_ids:
        att = attachments.get(att_id)
        if not att or not is_image(att["file_name"]) or "local" in att:
            continue
        rel = Path("assets") / week / f"{att_id}-{att['file_name']}"
        att["local"] = rel.as_posix() if att.get("url") and _download(att["url"], digest_dir / rel) else None
