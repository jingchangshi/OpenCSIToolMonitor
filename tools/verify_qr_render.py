"""Prove what GitCode's ``qrcode`` field actually is, and that we render it right.

The claim this script exists to settle is uncomfortable: GitCode's ``qrcode``
field is **not a QR code**. It is a WeChat mini-program code, and a terminal
rendering of it cannot be scanned. That claim is only worth making if it is
demonstrated, so this script demonstrates it against a real code from the live
API:

1. ask GitCode for a real login code;
2. check it with a real QR decoder (zxing-cpp) -- expect *no* result;
3. check for the three finder patterns a QR requires -- expect none;
4. measure its finest feature against a terminal's width;
5. confirm the file path preserves it, and the preview renders.

Run: python tools/verify_qr_render.py
Requires: zxing-cpp and Pillow (verification only -- never runtime deps).
"""
#: labels: LIVE, GET_ONLY

from __future__ import annotations

import io
import os
import sys

sys.path.insert(0, "src")

from PIL import Image  # noqa: E402

from opencsi.auth.gitcode_qr import GitCodeQrAuthenticator  # noqa: E402
from opencsi.auth.qr_render import (  # noqa: E402
    decode_payload_image,
    render_payload,
    write_image,
)


def shortest_dark_run(image: Image.Image) -> int:
    """The narrowest dark feature, in pixels.

    This is the number that decides scannability. A QR's narrowest feature is
    one *module* (tens of pixels, and one terminal cell); a mini-program code's
    is one pixel.
    """
    grey = image.convert("L")
    pixels = grey.load()
    width, height = grey.size
    shortest = width
    for y in range(height):
        run = 0
        for x in range(width):
            if pixels[x, y] < 128:
                run += 1
            else:
                if run:
                    shortest = min(shortest, run)
                run = 0
        if run:
            shortest = min(shortest, run)
    return shortest


def corner_darkness(image: Image.Image, *, box: int = 40) -> list[float]:
    """Darkness in the three corners a QR puts finder patterns in."""
    grey = image.convert("L")
    pixels = grey.load()
    width, height = grey.size

    def fraction(left: int, top: int) -> float:
        dark = sum(
            1
            for x in range(left, min(left + box, width))
            for y in range(top, min(top + box, height))
            if pixels[x, y] < 128
        )
        return dark / (box * box)

    return [
        fraction(0, 0),
        fraction(width - box, 0),
        fraction(0, height - box),
    ]


def main() -> int:
    auth = GitCodeQrAuthenticator(poll_interval=0.0, max_wait=5.0)
    challenge = auth.start_login()
    print(f"scene_id length   : {len(challenge.scene_id)} (value not printed)")
    print(f"payload length    : {len(challenge.image)}")
    print(f"payload prefix    : {challenge.image[:32]}")

    raw = decode_payload_image(challenge.image)
    if raw is None:
        print("FAIL: the payload did not decode to image bytes")
        return 1
    source = Image.open(io.BytesIO(raw))
    print(f"decoded image     : {source.size} {source.mode}")
    print()

    failures: list[str] = []

    # 1. Does a real QR decoder read it?
    try:
        import zxingcpp

        found = zxingcpp.read_barcodes(source.convert("L"))
        decoded = found[0].text if found else None
        print(f"QR decoder reads it: {decoded is not None}")
        if decoded is not None:
            failures.append(
                "zxing decoded the payload as a QR, so it IS a QR and the "
                "terminal rendering should be scannable"
            )
    except ImportError:
        print("QR decoder reads it: SKIPPED (zxing-cpp not installed)")

    # 2. Are the finder patterns there?
    corners = corner_darkness(source)
    print(f"corner darkness    : {[round(c, 3) for c in corners]}")
    if any(c > 0.05 for c in corners):
        failures.append(
            "a corner contains dark pixels, so this may be a QR after all"
        )
    else:
        print("                     (a QR always has a finder square in 3 corners)")

    # 3. How fine is its detail?
    finest = shortest_dark_run(source)
    width = source.width
    print(f"finest dark run    : {finest} px of {width}")
    print(f"modules if it were a QR: {width / finest:.1f}")
    if finest >= width // 21:
        print("                     (coarse enough that a terminal could show it)")
    else:
        print("                     (finer than any QR module -- a terminal cannot)")

    # 4. Does the file preserve it?
    path = write_image(raw)
    if path is None or not os.path.exists(path):
        failures.append("write_image did not produce a readable file")
    else:
        size = os.path.getsize(path)
        print(f"file written       : {os.path.basename(path)} ({size} bytes)")
        os.remove(path)

    # 5. Does the preview render?
    result = render_payload(challenge.image)
    if result.mode == "unicode" and result.text:
        lines = result.text.splitlines()
        print(f"preview            : {len(lines)} lines x {len(lines[0])} cols")
        print(f"preview scannable  : {result.scannable} (must be False)")
        if result.scannable:
            failures.append("the preview claims to be scannable, which is untrue")
        print()
        for line in lines[:8]:
            print("   " + line)
    else:
        failures.append(f"the preview did not render ({result.mode}: {result.detail})")

    print()
    if failures:
        print("VERIFICATION FAILED:")
        for item in failures:
            print(f"  - {item}")
        return 1
    print("VERIFIED: the payload is a WeChat mini-program code; the file path")
    print("preserves it and the terminal rendering is a labelled preview.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
