"""How much camera tilt the photo path survives, without needing Vision.

Takes a PDF page's real word boxes, groups them into phrase fragments the way Vision
reports text, and rotates them as a tilted photo would - Vision's boxes stay upright
around rotated text, while the line's own angle is sent alongside. The "tilt" cases
in scripts/eval_extraction.py score the result; this prints the text the backend
would receive:

    .venv/bin/python eval/tools/tilt_sim.py report.pdf --page 1 --degrees 6
"""

import math
import sys
from pathlib import Path

import pdfplumber

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))
from eval_extraction import layout_text_from_blocks  # noqa: E402


def fragments(page) -> list[dict]:
    """Words joined into phrases: same line, gap under ~1.5 characters."""
    words = page.extract_words(keep_blank_chars=False, use_text_flow=False)
    words.sort(key=lambda w: (round(w["top"] / 3), w["x0"]))
    out: list[dict] = []
    for w in words:
        last = out[-1] if out else None
        size = w["bottom"] - w["top"]
        if last and abs(last["top"] - w["top"]) < size * 0.4 and 0 <= w["x0"] - last["x1"] < size * 0.8:
            last["text"] += " " + w["text"]
            last["x1"], last["bottom"] = w["x1"], max(last["bottom"], w["bottom"])
        else:
            out.append(dict(text=w["text"], x0=w["x0"], x1=w["x1"], top=w["top"], bottom=w["bottom"]))
    return out


def tilted_blocks(frags, width, height, degrees, with_angle=False) -> list[dict]:
    t = math.radians(degrees)
    cx, cy = width / 2, height / 2
    blocks = []
    for f in frags:
        corners = [(f["x0"], f["top"]), (f["x1"], f["top"]), (f["x0"], f["bottom"]), (f["x1"], f["bottom"])]
        pts = [
            (
                cx + (x - cx) * math.cos(t) - (y - cy) * math.sin(t),
                cy + (x - cx) * math.sin(t) + (y - cy) * math.cos(t),
            )
            for x, y in corners
        ]
        xs, ys = [p[0] for p in pts], [p[1] for p in pts]
        block = dict(text=f["text"], x=min(xs) / width, y=min(ys) / height,
                     width=(max(xs) - min(xs)) / width, height=(max(ys) - min(ys)) / height)
        if with_angle:
            (tlx, tly), (trx, try_), (blx, bly) = pts[0], pts[1], pts[2]
            block.update(
                angle=math.atan2(try_ - tly, trx - tlx),
                cx=sum(xs) / 4 / width, cy=sum(ys) / 4 / height,
                length=math.hypot(trx - tlx, try_ - tly) / width,
                lineHeight=math.hypot(blx - tlx, bly - tly) / height,
                aspect=width / height,
            )
        blocks.append(block)
    return blocks


def main() -> None:
    import argparse

    ap = argparse.ArgumentParser()
    ap.add_argument("pdf")
    ap.add_argument("--page", type=int, default=1)
    ap.add_argument("--degrees", type=float, default=0)
    args = ap.parse_args()
    with pdfplumber.open(args.pdf) as doc:
        page = doc.pages[args.page - 1]
        blocks = tilted_blocks(fragments(page), page.width, page.height, args.degrees, with_angle=True)
    print(layout_text_from_blocks(blocks))


if __name__ == "__main__":
    main()
