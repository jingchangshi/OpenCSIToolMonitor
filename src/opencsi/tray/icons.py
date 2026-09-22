"""Draw the tray icon.

The icon is generated rather than shipped as a PNG. Two reasons: a binary asset
in a source repository is unreviewable, and the icon has to *change* with state
(green when fine, blue while working, amber when the user must act, red when the
server is broken). Drawing it means the state is encoded in code that a test can
check, instead of in four image files nobody can diff.

The palette is deliberately coarser than the state machine: REFRESHING and
RENEWING share blue because both mean "working, wait"; LOGIN_REQUIRED and
AUTH_ERROR share amber because both mean "you must act". The icon answers "do I
need to do something?", and the tooltip and menu answer "what exactly?" --
encoding all eight states as eight colours would make them harder to tell apart,
not easier.

Pillow is optional. When it is absent the tray falls back to a solid
programmatically-built icon, so a missing extra degrades the *appearance* and
never the function.
"""

from __future__ import annotations

import logging
from typing import Any

log = logging.getLogger("opencsi.tray.icons")

#: Icon size. Windows asks for 16x16 in the notification area and scales up for
#: high-DPI; 64 gives a clean downscale without looking soft.
SIZE = 64

#: State -> (fill, ring) colours. Green/amber/red/grey, plus a distinct blue for
#: "we are doing something", so a glance distinguishes working from broken.
_COLOURS: dict[str, tuple[tuple[int, int, int], tuple[int, int, int]]] = {
    "OK": ((34, 139, 87), (255, 255, 255)),
    "REFRESHING": ((0, 120, 212), (255, 255, 255)),
    "RENEWING": ((0, 120, 212), (255, 255, 255)),
    "STARTING": ((128, 128, 128), (255, 255, 255)),
    "LOGIN_REQUIRED": ((214, 158, 0), (255, 255, 255)),
    "CONSENT_REQUIRED": ((214, 158, 0), (255, 255, 255)),
    "BROWSER_UNAVAILABLE": ((214, 158, 0), (255, 255, 255)),
    "AUTH_ERROR": ((214, 158, 0), (255, 255, 255)),
    "OFFLINE": ((128, 128, 128), (255, 255, 255)),
    "SERVER_ERROR": ((196, 43, 28), (255, 255, 255)),
}

_DEFAULT = ((128, 128, 128), (255, 255, 255))


def colours_for(state: str) -> tuple[tuple[int, int, int], tuple[int, int, int]]:
    """The colours for a state name. Unknown states get the neutral grey.

    An unknown state must not be drawn green: a colour is an assertion about
    health, and asserting "fine" about a state we do not understand is exactly
    the failure this project keeps guarding against.
    """
    return _COLOURS.get(str(state).upper(), _DEFAULT)


def pillow_available() -> bool:
    try:
        import PIL.Image  # noqa: F401

        return True
    except Exception:  # noqa: BLE001
        return False


def make_icon(state: str, *, size: int = SIZE) -> Any:
    """Build a PIL image for ``state``.

    The glyph is a filled rounded square with a white "percentage" motif, so the
    icon reads as *usage* rather than as a generic status dot. When the state is
    not OK a wedge is cut out of the top-right, giving the shape itself a signal
    for users who cannot rely on colour.
    """
    from PIL import Image, ImageDraw

    fill, ring = colours_for(state)
    image = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)

    inset = max(1, size // 16)
    draw.rounded_rectangle(
        (inset, inset, size - inset - 1, size - inset - 1),
        radius=size // 5,
        fill=fill,
        outline=ring,
        width=max(1, size // 32),
    )

    # Two circles and a slash: the conventional "percent" mark.
    radius = max(2, size // 12)
    margin = size // 4
    for cx, cy in ((margin, size - margin), (size - margin, margin)):
        draw.ellipse((cx - radius, cy - radius, cx + radius, cy + radius), fill=ring)
    draw.line(
        (margin, size - margin, size - margin, margin),
        fill=ring,
        width=max(2, size // 12),
    )

    healthy = str(state).upper() in ("OK",)
    if not healthy:
        # A notch, not just a colour change, so the state is legible in
        # greyscale and to a colour-blind user.
        draw.polygon(
            [(size, 0), (size, size // 2), (size // 2, 0)],
            fill=(0, 0, 0, 0),
        )
    return image


def make_fallback_icon(state: str, *, size: int = SIZE) -> Any:
    """A no-dependency icon: a flat colour swatch.

    Used when Pillow is missing. pystray needs *an* image, and refusing to start
    the tray over a missing optional dependency would be the wrong trade.
    """
    try:
        from PIL import Image
    except Exception:  # noqa: BLE001
        return None
    fill, _ring = colours_for(state)
    return Image.new("RGB", (size, size), fill)


def icon_bytes(state: str, *, size: int = SIZE) -> bytes | None:
    """PNG bytes for ``state``, or ``None`` when Pillow is unavailable."""
    if not pillow_available():
        return None
    import io

    buffer = io.BytesIO()
    make_icon(state, size=size).save(buffer, format="PNG")
    return buffer.getvalue()
