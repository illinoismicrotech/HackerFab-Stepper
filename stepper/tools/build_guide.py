"""Build docs/HOW-TO-GUIDE.html (one self-contained file that works offline) from
docs/guide_template.html and the screenshots in docs/guide_images/.

Edit the text in the template, then run from the stepper/ folder:

    python tools/build_guide.py

In the template, a screenshot is written as   IMG:<name>|<caption>   inside a <figure>; the
picture is docs/guide_images/<name>.png (or .jpg) and is embedded into the guide.
"""
import base64
import re
import struct
import sys
from pathlib import Path

DOCS = Path(__file__).resolve().parents[1] / "docs"
TEMPLATE = DOCS / "guide_template.html"
IMAGES = DOCS / "guide_images"
GUIDE = DOCS / "HOW-TO-GUIDE.html"


def image_size(data: bytes) -> tuple[int, int]:
    if data.startswith(b"\x89PNG"):
        return struct.unpack(">II", data[16:24])
    i = 2  # JPEG: walk the segments to the frame header
    while i < len(data):
        marker, length = data[i + 1], struct.unpack(">H", data[i + 2:i + 4])[0]
        if marker in (0xC0, 0xC1, 0xC2):
            height, width = struct.unpack(">HH", data[i + 5:i + 9])
            return width, height
        i += 2 + length
    raise ValueError("unknown image format")


def figure(match: re.Match) -> str:
    name, caption = match.group(1), match.group(2)
    for suffix, mime in ((".png", "png"), (".jpg", "jpeg")):
        path = IMAGES / (name + suffix)
        if path.exists():
            break
    else:
        sys.exit(f"Missing screenshot: {IMAGES / name}.png (or .jpg)")
    data = path.read_bytes()
    width, height = image_size(data)
    encoded = base64.b64encode(data).decode("ascii")
    return (f'<img src="data:image/{mime};base64,{encoded}" width="{width}" height="{height}" alt="{caption}" '
            f'style="max-width:{width}px"><figcaption>{caption}</figcaption>')


def main():
    html = re.sub(r"IMG:([a-z0-9_]+)\|([^<]*)", figure, TEMPLATE.read_text(encoding="utf-8"))
    GUIDE.write_text(html, encoding="utf-8", newline="")
    print(f"Wrote {GUIDE} ({GUIDE.stat().st_size / 1e6:.1f} MB)")


if __name__ == "__main__":
    main()
