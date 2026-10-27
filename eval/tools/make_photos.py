"""Renders report PDFs as the images users actually upload: a clean scan and two phone
photos of a printed page (tilted, unevenly lit, softer, JPEG-compressed).

    .venv/bin/python eval/tools/make_photos.py eval/private/*.pdf

Writes eval/private/photos/<pdf>_p<page>_<variant>.jpg. Synthetic, so the same
degradation can be re-run after every change - real phone photos still belong in the
eval set as well, since no simulation covers glare, folds or a thumb in the frame.
"""

import sys
from pathlib import Path

import pypdfium2 as pdfium
from PIL import Image, ImageDraw, ImageFilter

OUT = Path(__file__).resolve().parent.parent / "private" / "photos"

# name: (tilt degrees, output width px, blur radius, JPEG quality, shadow strength)
VARIANTS = {
    "scan": (0.0, 1700, 0.0, 92, 0.0),
    "phone": (2.5, 1280, 0.8, 75, 0.25),
    "rough": (6.0, 960, 1.4, 55, 0.45),
}


def degrade(page: Image.Image, tilt: float, width: int, blur: float, quality: int, shadow: float):
    img = page.convert("RGB")
    if shadow:
        # Light falling off across the page, as under a desk lamp.
        mask = Image.linear_gradient("L").rotate(35, expand=True).resize(img.size)
        dark = Image.new("RGB", img.size, (60, 55, 50))
        img = Image.composite(dark, img, mask.point(lambda v: int(v * shadow)))
    if tilt:
        # The page on a darker desk, slightly rotated in the frame.
        pad = int(img.width * 0.06)
        desk = Image.new("RGB", (img.width + 2 * pad, img.height + 2 * pad), (92, 84, 74))
        desk.paste(img, (pad, pad))
        img = desk.rotate(tilt, resample=Image.BICUBIC, fillcolor=(92, 84, 74))
    if blur:
        img = img.filter(ImageFilter.GaussianBlur(blur))
    img = img.resize((width, int(img.height * width / img.width)), Image.LANCZOS)
    ImageDraw.Draw(img)  # keeps PIL from lazily deferring the resize
    return img, quality


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    for pdf_path in map(Path, sys.argv[1:]):
        pdf = pdfium.PdfDocument(str(pdf_path))
        for i, page in enumerate(pdf, start=1):
            rendered = page.render(scale=200 / 72).to_pil()
            for name, params in VARIANTS.items():
                img, quality = degrade(rendered, *params)
                target = OUT / f"{pdf_path.stem}_p{i}_{name}.jpg"
                img.save(target, quality=quality)
                print(target.relative_to(OUT.parent.parent))


if __name__ == "__main__":
    main()
