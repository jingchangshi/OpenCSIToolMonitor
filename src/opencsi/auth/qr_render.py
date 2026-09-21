"""Render a QR code in a terminal.

GitCode hands back the QR as an *image* (a ``data:`` URI or an https URL), so
displaying it means decoding that image and painting it with Unicode blocks --
not encoding a QR ourselves. That is why this module is mostly about image
decoding rather than about QR error correction.

Two rungs, in order:

1. **Unicode half-blocks** with Pillow (the ``qr`` extra). Each character cell
   carries two vertical pixels via ``▀`` / ``▄`` / ``█`` / space. Because a
   terminal cell is roughly twice as tall as it is wide, two pixels per cell
   makes the printed code approximately square -- which matters, because a QR
   with the wrong aspect ratio often will not scan.
2. **Write the image to a file** and tell the user where it is. This needs no
   third-party library at all and always works, so the feature degrades instead
   of disappearing when Pillow is absent.

The ``segno`` dependency is only used for the case where the server hands back
a *string to encode* rather than an image. It is not needed for the flow GitCode
actually implements, and the code says so rather than silently requiring it.

Nothing here logs or returns a secret: the QR payload for the login flow is a
``scene_id`` bearer value, so the *encoded content* is never printed, only the
picture of it.
"""

from __future__ import annotations

import base64
import binascii
import io
import logging
import os
import re
import tempfile
from dataclasses import dataclass
from typing import Any, Callable

log = logging.getLogger("opencsi.auth.qr_render")

#: A QR code needs a quiet zone to scan reliably; four modules is the spec.
QUIET_ZONE = 4

#: Upper/lower half-block glyphs. Chosen over full blocks because they double
#: the vertical resolution, which is what keeps the code square.
_UPPER = "▀"
_LOWER = "▄"
_FULL = "█"
_EMPTY = " "

_DATA_URI = re.compile(r"^data:(?P<mime>[\w/+.-]+)?;?(?P<encoding>base64)?,(?P<payload>.*)$", re.S)


@dataclass(frozen=True)
class RenderResult:
    """How the QR was displayed (secret-free)."""

    mode: str
    """``"unicode"``, ``"file"`` or ``"none"``."""

    text: str = ""
    """The block art, when ``mode == "unicode"``."""

    path: str | None = None
    """Where the image was written, when ``mode == "file"``."""

    detail: str | None = None

    @property
    def ok(self) -> bool:
        return self.mode != "none"


def pillow_available() -> bool:
    """Whether the image path can be used."""
    try:
        import PIL.Image  # noqa: F401

        return True
    except Exception:  # noqa: BLE001
        return False


def segno_available() -> bool:
    """Whether a plain string could be encoded locally, if that were needed."""
    try:
        import segno  # noqa: F401

        return True
    except Exception:  # noqa: BLE001
        return False


def decode_payload_image(
    payload: str, *, fetch: "Callable[[str], bytes | None] | None" = None
) -> bytes | None:
    """Turn GitCode's ``qrcode`` field into image bytes.

    Accepts the shapes the field can take: a ``data:`` URI, a bare base64 blob,
    or an https URL. Returns ``None`` when the payload is none of those -- in
    which case it is probably a *string to encode* rather than an image, and
    the caller falls back to :func:`encode_text`.

    ``fetch`` is injectable so the suite never touches the network. Without it
    the module would be untestable offline, and a test that quietly reached out
    to a live CDN is exactly the kind of thing this suite forbids.
    """
    if not payload:
        return None
    text = payload.strip()

    match = _DATA_URI.match(text)
    if match and match.group("encoding"):
        try:
            return base64.b64decode(match.group("payload"), validate=False)
        except (binascii.Error, ValueError):
            return None

    if text.startswith(("http://", "https://")):
        fetched = (fetch or _fetch)(text)
        # Validate whatever came back, not just what the default fetcher
        # returned: an injected fetcher (or a CDN answering 200 with an HTML
        # error page) must not be able to smuggle non-image bytes through.
        if fetched is None or not looks_like_image(fetched):
            return None
        return fetched

    # A bare base64 blob: PNGs start with the bytes \x89PNG, which is "iVBOR"
    # once base64-encoded; JPEG is "/9j/"; GIF is "R0lGOD".
    if text.startswith(("iVBOR", "/9j/", "R0lGOD")):
        try:
            return base64.b64decode(text, validate=False)
        except (binascii.Error, ValueError):
            return None
    return None


def _fetch(url: str, *, timeout: float = 20.0) -> bytes | None:
    """Fetch an image URL, bypassing any proxy.

    The openCsiTool API honours the system proxy, but a local proxy that cannot
    reach a CDN must not make the login QR unrenderable.

    The response is checked for image magic bytes before being accepted. An
    SPA-style CDN happily answers a request for a missing asset with a full
    HTML page and ``200``, and passing that on as "the QR image" produces a
    baffling failure far from its cause.
    """
    import urllib.error
    import urllib.request

    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(url, timeout=timeout) as response:
            payload = response.read()
    except (urllib.error.URLError, OSError, TimeoutError) as exc:
        log.debug("could not fetch the QR image: %s", type(exc).__name__)
        return None
    if not looks_like_image(payload):
        log.debug("the QR image URL returned something that is not an image")
        return None
    return payload


#: Magic bytes for the image formats a QR is realistically delivered as.
_IMAGE_MAGIC = (b"\x89PNG\r\n\x1a\n", b"\xff\xd8\xff", b"GIF87a", b"GIF89a", b"BM")


def looks_like_image(payload: bytes) -> bool:
    """Whether ``payload`` starts with a known image signature."""
    return any(payload.startswith(magic) for magic in _IMAGE_MAGIC)


def render_image(
    image_bytes: bytes,
    *,
    max_width: int = 0,
    quiet_zone: int = QUIET_ZONE,
) -> RenderResult:
    """Render image bytes as Unicode half-blocks.

    Returns ``mode="none"`` when Pillow is unavailable or the bytes are not a
    decodable image, so the caller can fall back to writing a file.
    """
    try:
        from PIL import Image
    except Exception:  # noqa: BLE001 - optional dependency
        return RenderResult(
            "none",
            detail=(
                "Pillow is not installed, so the QR cannot be drawn in the "
                'terminal. Install the extra: pip install "opencsi[qr]"'
            ),
        )

    try:
        image = Image.open(io.BytesIO(image_bytes))
        image.load()
    except Exception as exc:  # noqa: BLE001 - not an image we understand
        return RenderResult(
            "none", detail=f"the QR payload was not a decodable image ({type(exc).__name__})"
        )

    return render_pil_image(image, max_width=max_width, quiet_zone=quiet_zone)


def render_pil_image(
    image: Any, *, max_width: int = 0, quiet_zone: int = QUIET_ZONE
) -> RenderResult:
    """Render an already-decoded Pillow image as Unicode half-blocks."""
    from PIL import Image

    # Binarise: a QR is black on white, and thresholding removes any JPEG
    # ringing that would otherwise print as a grey speckle.
    if image.mode != "L":
        image = image.convert("L")
    image = image.point(lambda value: 255 if value > 128 else 0, mode="1")

    # Trim any whitespace the server already added, then apply our own quiet
    # zone, so the margin is exactly what the spec asks for.
    box = image.getbbox()
    if box:
        image = image.crop(box)

    side = max(image.width, image.height)
    if side <= 0:
        return RenderResult("none", detail="the QR image was empty")

    # Terminal cells are about twice as tall as wide, so sample two rows per
    # printed row. ``side`` is used for both axes to keep the code square.
    width = image.width
    if max_width and width > max_width:
        width = max_width
    rows = max(1, width)
    target = (width, rows * 2)
    image = image.resize(target, Image.Resampling.NEAREST)

    padded = Image.new("1", (width + quiet_zone * 2, rows * 2 + quiet_zone * 2), 1)
    padded.paste(image, (quiet_zone, quiet_zone))

    pixels = padded.load()
    lines: list[str] = []
    for row in range(0, padded.height, 2):
        chars: list[str] = []
        for col in range(padded.width):
            top_dark = pixels[col, row] == 0
            bottom_dark = row + 1 < padded.height and pixels[col, row + 1] == 0
            if top_dark and bottom_dark:
                chars.append(_FULL)
            elif top_dark:
                chars.append(_UPPER)
            elif bottom_dark:
                chars.append(_LOWER)
            else:
                chars.append(_EMPTY)
        lines.append("".join(chars))
    return RenderResult("unicode", text="\n".join(lines))


def encode_text(text: str, *, scale: int = 0) -> RenderResult:
    """Encode ``text`` as a QR locally and render it.

    Only needed if the server ever returns a string to encode instead of an
    image. Requires the ``segno`` extra.
    """
    try:
        import segno
    except Exception:  # noqa: BLE001 - optional dependency
        return RenderResult(
            "none",
            detail=(
                "this QR payload is a string rather than an image, and segno "
                'is not installed to encode it: pip install "opencsi[qr]"'
            ),
        )

    # Register before encoding: this is the login bearer value.
    from ..redaction import register_secret

    register_secret(text)

    try:
        code = segno.make(text, error="m")
    except Exception as exc:  # noqa: BLE001
        return RenderResult("none", detail=f"could not encode the QR ({type(exc).__name__})")

    buffer = io.BytesIO()
    code.save(buffer, kind="png", scale=max(1, scale or 4), border=0)
    return render_image(buffer.getvalue())


def render_payload(payload: str, *, max_width: int = 0) -> RenderResult:
    """Render GitCode's ``qrcode`` field, whichever shape it arrives in."""
    if not payload:
        return RenderResult("none", detail="the server returned no QR payload")

    image_bytes = decode_payload_image(payload)
    if image_bytes is not None:
        result = render_image(image_bytes, max_width=max_width)
        if result.ok:
            return result
        # Fall through to the file path so a missing Pillow still leaves the
        # user with a scannable QR rather than nothing.
        written = write_temp_image(image_bytes)
        if written is not None:
            return RenderResult(
                "file",
                path=written,
                detail=result.detail,
            )
        return result

    # Not an image: it must be content to encode.
    return encode_text(payload)


def write_temp_image(image_bytes: bytes, *, directory: str | None = None) -> str | None:
    """Write image bytes to a temp file and return the path.

    Used as the no-dependency fallback. The file is the *login* QR, which is
    single-use and expires in minutes, so it is not a durable secret -- but it
    is still written with owner-only permissions where the platform supports it.
    """
    try:
        handle, path = tempfile.mkstemp(
            prefix="opencsi-qr-", suffix=".png", dir=directory or _temp_dir()
        )
    except OSError:
        return None
    try:
        with os.fdopen(handle, "wb") as fh:
            fh.write(image_bytes)
        try:
            os.chmod(path, 0o600)
        except OSError:  # pragma: no cover - Windows ACLs
            pass
    except OSError:
        return None
    return path


def _temp_dir() -> str | None:
    """A per-user scratch directory, when one is available.

    On Windows ``%LOCALAPPDATA%`` is per-user; falling back to the system temp
    directory is fine because the file is short-lived and non-durable.
    """
    local = os.environ.get("LOCALAPPDATA")
    if local:
        candidate = os.path.join(local, "OpenCSI", "qr")
        try:
            os.makedirs(candidate, exist_ok=True)
            return candidate
        except OSError:
            pass
    return None
