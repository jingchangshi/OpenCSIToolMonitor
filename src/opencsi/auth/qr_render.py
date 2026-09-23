"""Display GitCode's login code in a terminal, honestly.

The finding that shapes this module
-----------------------------------
GitCode's ``qrcode`` response field does **not** contain a QR code. It contains
a **WeChat mini-program code** (微信小程序码): a radial dot pattern with the
GitCode logo in the middle and a WeChat badge in the corner, drawn in GitCode
red (#DB203F) and WeChat green (#07C160).

That was established by decoding a real one, not by reading documentation:

* a QR decoder (zxing-cpp) returns nothing for it;
* it has no finder patterns -- the three corner squares a QR requires are simply
  absent (0% dark pixels in all three corners);
* its shortest dark feature is **one pixel** in a 430 px image.

The last point is decisive for rendering. A terminal is roughly 80-120 columns;
downscaling 430 px to that width destroys sub-pixel features, so a terminal
rendering of this code **cannot be scanned** -- by WeChat or by anything else. A
QR survives downscaling because it is 21-177 modules of solid blocks; a
mini-program code does not, because its dots are finer than the grid.

So this module does not pretend. It offers two things, in priority order:

1. **The file** (:func:`write_image`) -- the full-resolution PNG, written where
   the user can open it and scan it with WeChat. This always works and is the
   path ``opencsi login --qr`` leads with.
2. **A terminal preview** (:func:`render_payload`) -- block art that shows the
   shape of the code so the user can tell it loaded and see what they are about
   to scan. It is labelled a preview, because calling it scannable would be a
   lie the user only discovers when their phone refuses to read it.

WeChat's scanner reads these codes from a screen or a printout, so the file path
is a genuine pure-CLI login route: no browser, no DevTools, no DOM.
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

#: Terminal columns for the preview. Wide enough to show the pattern clearly in
#: an 80-column terminal, narrow enough to fit one.
PREVIEW_WIDTH = 45

#: The smallest width at which the preview still resembles the code.
MIN_PREVIEW_WIDTH = 21

_DATA_URI = re.compile(
    r"^data:(?P<mime>[\w/+.-]+)?;?(?P<encoding>base64)?,(?P<payload>.*)$", re.S
)

#: Magic bytes for the formats a code image is realistically delivered as.
_IMAGE_MAGIC = (b"\x89PNG\r\n\x1a\n", b"\xff\xd8\xff", b"GIF87a", b"GIF89a", b"BM")


@dataclass(frozen=True)
class RenderResult:
    """How the code was displayed (secret-free).

    ``scannable`` is the field that matters. It is ``True`` only when the
    rendering is at a resolution that can actually be read by a scanner, which
    for a mini-program code means the full-resolution file -- never the block
    art. A caller that shows a preview without saying so is telling the user
    something untrue.
    """

    mode: str
    """``"file"``, ``"unicode"`` or ``"none"``."""

    text: str = ""
    """Block art, when the mode includes a preview."""

    path: str | None = None
    """Where the full-resolution image was written, when it was."""

    scannable: bool = False
    """Whether this rendering can actually be scanned."""

    detail: str | None = None

    @property
    def ok(self) -> bool:
        return self.mode != "none"


def pillow_available() -> bool:
    try:
        import PIL.Image  # noqa: F401

        return True
    except Exception:  # noqa: BLE001
        return False


def segno_available() -> bool:
    """Whether a plain string could be encoded locally, if that were needed.

    GitCode does not need this -- it returns an image -- but a payload that
    turned out to be content-to-encode would, and the CLI says which extra is
    missing rather than failing obscurely.
    """
    try:
        import segno  # noqa: F401

        return True
    except Exception:  # noqa: BLE001
        return False


def looks_like_image(payload: bytes) -> bool:
    """Whether ``payload`` starts with a known image signature."""
    return any(payload.startswith(magic) for magic in _IMAGE_MAGIC)


def decode_payload_image(
    payload: str, *, fetch: "Callable[[str], bytes | None] | None" = None
) -> bytes | None:
    """Turn GitCode's ``qrcode`` field into image bytes.

    Accepts a ``data:`` URI, a bare base64 blob, or an https URL. Returns
    ``None`` when the payload is none of those, in which case it is probably a
    string to encode and the caller falls back to :func:`encode_text`.

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
        # returned: an SPA CDN answers a request for a missing asset with a full
        # HTML page and ``200``, and passing that on as "the code image"
        # produces a baffling failure far from its cause.
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
    reach a CDN must not make the login code unrenderable.
    """
    import urllib.error
    import urllib.request

    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(url, timeout=timeout) as response:
            payload = response.read()
    except (urllib.error.URLError, OSError, TimeoutError) as exc:
        log.debug("could not fetch the login code image: %s", type(exc).__name__)
        return None
    if not looks_like_image(payload):
        log.debug("the login code URL returned something that is not an image")
        return None
    return payload


def render_image(
    image_bytes: bytes, *, max_width: int = 0, quiet_zone: int = 0
) -> RenderResult:
    """Render image bytes as Unicode block art (a *preview*, not scannable)."""
    try:
        from PIL import Image
    except Exception:  # noqa: BLE001 - optional dependency
        return RenderResult(
            "none",
            detail=(
                "Pillow is not installed, so the code cannot be drawn in the "
                'terminal. Install the extra: pip install "opencsi[qr]"'
            ),
        )
    try:
        image = Image.open(io.BytesIO(image_bytes))
        image.load()
    except Exception as exc:  # noqa: BLE001 - not an image we understand
        return RenderResult(
            "none",
            detail=f"the code payload was not a decodable image ({type(exc).__name__})",
        )
    return render_pil_image(image, max_width=max_width, quiet_zone=quiet_zone)


def render_pil_image(
    image: Any, *, max_width: int = 0, quiet_zone: int = 0
) -> RenderResult:
    """Render a decoded image as Unicode half-blocks.

    Deliberately *not* thresholded to pure black and white. This code's
    information is in fine dots, and binarising a downscaled version of it
    throws away the anti-aliased grey that carries the pattern's structure --
    producing a speckled mess that looks like noise rather than a code.

    It is drawn as a luminance ramp instead (``░``/``▒``/``▓``/``█``), which
    keeps the visual structure legible at terminal resolution. That is the honest
    goal here: show the user the code loaded and what it looks like. Scanning
    happens from the file, because at 45 columns the dots are gone.
    """
    from PIL import Image

    if image.mode not in ("L", "RGB", "RGBA"):
        image = image.convert("RGBA")
    if image.mode == "RGBA":
        # Composite onto white: a transparent PNG would otherwise render as a
        # black field, which is the opposite of what the source shows.
        background = Image.new("RGBA", image.size, (255, 255, 255, 255))
        image = Image.alpha_composite(background, image)
    grey = image.convert("L")

    width, height = grey.size
    if width <= 0 or height <= 0:
        return RenderResult("none", detail="the code image was empty")

    size = max(MIN_PREVIEW_WIDTH, max_width or PREVIEW_WIDTH)
    size = min(size, width)
    # Preserve the aspect ratio: the code is square, but a future payload might
    # not be, and squashing it would misrepresent the shape.
    scaled = grey.resize((size, max(1, round(size * height / width))), Image.Resampling.BOX)

    padded_width = scaled.width + quiet_zone * 2
    padded = Image.new("L", (padded_width, scaled.height + quiet_zone * 2), 255)
    padded.paste(scaled, (quiet_zone, quiet_zone))

    pixels = padded.load()
    # Four levels, darkest first. A QR would use two; this needs more because
    # the pattern is finer than the cell grid and the intermediate greys are
    # what make its structure visible at all.
    ramp = " .:-=+*#%@"
    lines: list[str] = []
    for row in range(0, padded.height, 2):
        chars: list[str] = []
        for col in range(padded.width):
            top = pixels[col, row]
            bottom = row + 1 < padded.height and pixels[col, row + 1]
            # Average the two rows this cell covers, then map to a glyph.
            value = top if bottom is False else (top + bottom) // 2
            index = (255 - value) * (len(ramp) - 1) // 255
            chars.append(ramp[index])
        lines.append("".join(chars))
    return RenderResult(
        "unicode",
        text="\n".join(lines),
        scannable=False,
        detail=(
            "this is a WeChat mini-program code, not a QR code; its dots are "
            "finer than a terminal cell, so scan it from the saved image"
        ),
    )


def encode_text(text: str, *, scale: int = 4) -> RenderResult:
    """Encode ``text`` as a QR locally and render it.

    Only reachable if the server ever returns a string to encode instead of an
    image. Requires the ``segno`` extra.
    """
    try:
        import segno
    except Exception:  # noqa: BLE001 - optional dependency
        return RenderResult(
            "none",
            detail=(
                "this payload is a string rather than an image, and segno is "
                'not installed to encode it: pip install "opencsi[qr]"'
            ),
        )

    from ..redaction import register_secret

    register_secret(text)

    try:
        code = segno.make(text, error="m")
    except Exception as exc:  # noqa: BLE001
        return RenderResult("none", detail=f"could not encode the code ({type(exc).__name__})")

    buffer = io.BytesIO()
    code.save(buffer, kind="png", scale=max(1, scale), border=0)
    result = render_image(buffer.getvalue())
    # A locally encoded QR *is* a real QR, so its block art is scannable at a
    # sufficient width -- unlike GitCode's mini-program code.
    return RenderResult(
        "unicode", text=result.text, scannable=True, detail=result.detail
    )


def render_payload(payload: str, *, max_width: int = 0) -> RenderResult:
    """Render GitCode's ``qrcode`` field, whichever shape it arrives in."""
    if not payload:
        return RenderResult("none", detail="the server returned no code payload")

    image_bytes = decode_payload_image(payload)
    if image_bytes is None:
        return encode_text(payload)

    result = render_image(image_bytes, max_width=max_width)
    if result.ok:
        return result

    # Pillow is missing or the bytes were undecodable. Writing the file is the
    # path that actually matters, so try it before giving up.
    written = write_image(image_bytes)
    if written is not None:
        return RenderResult("file", path=written, scannable=True, detail=result.detail)
    return result


def write_image(image_bytes: bytes, *, directory: str | None = None) -> str | None:
    """Write the full-resolution code to a file and return the path.

    This is the path that works. The file is the *login* code: single-use, and
    it expires in minutes, so it is not a durable secret -- but it is written
    with owner-only permissions where the platform supports it, and into a
    per-user directory rather than a shared temp folder.

    Older codes are pruned first. Without that, every ``login --qr`` leaves a
    file behind forever: a live run accumulated 14 of them, all long expired and
    none of them ever cleaned up. They are not dangerous, but an unbounded pile
    of credential images in a user's profile is not something to leave running.
    """
    target_dir = directory or _code_dir()
    # `TypeError` as well as `OSError`: `os.makedirs(None)` raises TypeError, not
    # OSError, so a caller that legitimately passed None (or a `_code_dir` that
    # somehow returned it) crashed here instead of falling through to mkstemp.
    # Catching both is what makes "no writable directory" a soft failure, which
    # is what the `return None` below has always been trying to express.
    try:
        os.makedirs(target_dir, exist_ok=True)
    except (OSError, TypeError, ValueError):
        target_dir = None
    try:
        handle, path = tempfile.mkstemp(
            prefix="opencsi-login-code-", suffix=".png", dir=target_dir
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
    # Pruned *after* writing, so the new file is counted among the kept ones.
    # Pruning first leaves ``keep`` old files plus this one. ``path`` is passed
    # as protected because same-tick mtimes sort arbitrarily.
    if target_dir:
        prune_codes(target_dir, protect=path)
    return path


#: How many previous login codes to keep. One is enough to scan; a couple of
#: spares cover a user who runs the command twice by accident.
KEEP_CODES = 3


def prune_codes(
    directory: str, *, keep: int = KEEP_CODES, protect: str | None = None
) -> int:
    """Delete all but the newest ``keep`` login codes. Returns how many went.

    ``protect`` is never deleted, whatever the ordering says. Two files written
    inside the same filesystem timestamp tick sort arbitrarily, and deleting the
    code that was just handed to the user would turn a housekeeping detail into
    a broken login.

    Failures are ignored: this is housekeeping, and refusing to show a login
    code because an unrelated old file could not be deleted would be a bad
    trade.
    """
    try:
        entries = [
            os.path.join(directory, name)
            for name in os.listdir(directory)
            if name.startswith("opencsi-login-code-") and name.endswith(".png")
        ]
    except OSError:
        return 0

    if len(entries) <= keep:
        return 0

    def modified(path: str) -> float:
        try:
            return os.path.getmtime(path)
        except OSError:
            return 0.0

    entries.sort(key=modified, reverse=True)
    survivors = set(entries[:keep])
    if protect and protect not in survivors:
        # Reserve a slot for the protected file by evicting the oldest survivor,
        # so the total never exceeds ``keep``.
        if len(survivors) >= keep:
            survivors.discard(entries[keep - 1])
        survivors.add(protect)

    removed = 0
    for path in entries:
        if path in survivors:
            continue
        try:
            os.remove(path)
            removed += 1
        except OSError:
            continue
    return removed


def _code_dir() -> str | None:
    """A per-user directory for the login code, when one is available."""
    # Ordered from most-private to least, and the last resort is a plain temp
    # directory rather than ``None``. Returning ``None`` was a crash, not a
    # graceful degradation: callers pass the result straight to ``os.makedirs``,
    # which raises **TypeError** for None -- and the caller below only catches
    # OSError. So on any machine without these variables set (an Ubuntu runner,
    # a stripped container, a bare CI shell) writing the login code raised
    # instead of falling back.
    #
    # ``HOME`` is checked after the cache variables because a cache directory is
    # the conventional home for a regenerable file like this, but any of them is
    # better than a world-readable temp directory.
    for variable in ("LOCALAPPDATA", "XDG_CACHE_HOME", "HOME"):
        base = os.environ.get(variable)
        if base:
            return os.path.join(base, "OpenCSI", "login-code")
    # Last resort: the system temp directory. Still per-file owner-only below.
    return os.path.join(tempfile.gettempdir(), "OpenCSI", "login-code")
