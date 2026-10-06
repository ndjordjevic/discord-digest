"""Download the image attachments a digest references, so they outlive Discord.

Discord CDN attachment URLs are signed and expire (~24h after export), so a
digest can't link to them. Images the summarizer picked are saved next to the
digest under `assets/<week>/`; other files (zips etc.) are only listed by name.
"""

from __future__ import annotations

import io
import time
import urllib.error
import urllib.request
from pathlib import Path

from PIL import Image, ImageOps, UnidentifiedImageError

from .parse import is_image

# One thumbnail per topic keeps the digest scannable (see render.py).
MAX_IMAGES_PER_TOPIC = 1
# Phone photos are often 2-3 MB; downscale before committing them to the repo.
MAX_IMAGE_SIDE = 800
DOWNLOAD_RETRIES = 3
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
    for attempt in range(DOWNLOAD_RETRIES):
        try:
            with urllib.request.urlopen(req, timeout=60) as resp:
                data = resp.read()
            break
        except urllib.error.HTTPError as e:
            # Expired/forbidden links won't recover; retrying them only looks like abuse to Discord.
            if e.code in (401, 403, 404) or attempt == DOWNLOAD_RETRIES - 1:
                return False
        except (urllib.error.URLError, TimeoutError):
            if attempt == DOWNLOAD_RETRIES - 1:
                return False
        time.sleep(2 ** attempt)
    tmp = dest.with_name(dest.name + ".tmp")  # atomic: a crash must not leave a corrupt "cached" file
    tmp.write_bytes(_shrink(data, dest.suffix.lower()))
    tmp.replace(dest)
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


def fetch_images(
    attachment_ids: list[str],
    attachments: dict[str, dict],
    digest_dir: Path,
    week: str,
    limit: int = MAX_IMAGES_PER_TOPIC,
) -> None:
    """Download up to `limit` of the picked image attachments; sets `local`
    (path relative to digest_dir) on success."""
    got = 0
    for att_id in attachment_ids:
        att = attachments.get(att_id)
        if got >= limit:
            break
        if not att or not is_image(att["file_name"]):
            continue
        if "local" not in att:
            rel = Path("assets") / week / f"{att_id}-{att['file_name']}"
            att["local"] = rel.as_posix() if att.get("url") and _download(att["url"], digest_dir / rel) else None
        got += bool(att["local"])
