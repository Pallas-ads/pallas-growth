# Task C: Creative image composition

Build the image half of the Creative Producer (PRD §4.1, role 6): read SAM 3.1's segmentation output, cut the product out of a product shot, and compose it over a background into the three placement sizes with the headline drawn inside Meta's safe zones. Pure image code with Pillow. No model calls, no network.

Read first: `AGENTS.md`, PRD §4.1 and §7.2 in `docs/PRD.md`, and `src/pgt/schemas/weekly.py` (use `CREATIVE_SIZES` from it; do not edit schemas).

## Files you may write

`src/pgt/creative/__init__.py` and `src/pgt/creative/compose.py`. Nothing else. The tests are `tests/unit/test_compose.py`; do not edit them. Another agent is editing `src/pgt/harness/verify.py` at the same time.

Pillow is installed (`PIL`, version 12 or later). Do not add packages.

## API

```python
from dataclasses import dataclass
from PIL import Image

SAFE_MARGINS: dict[str, tuple[int, int, int, int]]  # ratio -> (top, right, bottom, left) in output pixels

@dataclass(frozen=True)
class Box:
    x1: int; y1: int; x2: int; y2: int   # pixels in a w x h frame
    w: int; h: int                       # the frame the coordinates refer to

@dataclass(frozen=True)
class Rendered:
    image: Image.Image                    # RGB, exactly CREATIVE_SIZES[ratio]
    product_box: tuple[int, int, int, int]  # (x1, y1, x2, y2) where the product was pasted
    text_box: tuple[int, int, int, int]     # (x1, y1, x2, y2) bounding box of the headline band

def parse_sam_output(text: str) -> list[Box]
def cutout(image: Image.Image, box: Box, radius_ratio: float = 0.08) -> Image.Image
def make_demo_product_shot(size: int = 1600) -> tuple[Image.Image, Box]
def offline_background(prompt: str, size: tuple[int, int] = (1024, 1024)) -> Image.Image
def render_creative(background: Image.Image, product: Image.Image, headline: str, ratio: str) -> Rendered
def png_bytes(image: Image.Image) -> bytes
def save_png(image: Image.Image, path: str | Path) -> str   # returns sha256 hex of the written file
```

## Rules

### `parse_sam_output`

SAM 3.1 answers in plain text. Example of one detection:

```
<0f>0<|box;x1=497;y1=223;x2=1103;y2=1413;w=1600;h=1600|><|mask;x=0;y=0;data=1024,522,AAEC...|>
```

- Return one `Box` per `<|box;...|>` token, in order. Keys may come in any order inside the token. Ignore mask tokens and anything else.
- Raise `ValueError` if there is no box, or if a box is missing a key or has `x2 <= x1` or `y2 <= y1`.

### `cutout`

- The box may refer to a different frame than the image (for example the image was resized). Scale the coordinates by `image.width / box.w` and `image.height / box.h`, round to ints and clamp to the image.
- Crop to the box, convert to RGBA and apply a rounded-rectangle alpha mask with corner radius `radius_ratio * min(crop width, crop height)`: pixels outside the rounded corners get alpha 0, the inside alpha 255.

### `make_demo_product_shot`

A deterministic synthetic product shot, standing in for a real photo the user would upload.

- `size x size` RGB. A light, plain background (every pixel of the 10 px border identical).
- A phone mockup in the middle: dark rounded body, a lighter screen with a fake app UI for "Pallas Fit": the app name as text, a streak line such as "Day 12 streak", and two or three progress bars. Use `ImageFont.load_default(size=...)`. No emoji, no people.
- Return the image and the phone body's exact bounding box as a `Box` in that image's pixels (`w = h = size`). The phone body should span roughly 35-40% of the width and 70-75% of the height.
- Same `size` gives byte-identical output every time.

### `offline_background`

The offline stand-in for Muse Image. Deterministic per prompt: derive two colors from `sha256(prompt)` and draw a vertical gradient plus a few soft circles. RGB, exactly `size`. Different prompts should give different images. No text.

### `render_creative`

- `ratio` is a key of `CREATIVE_SIZES`; anything else raises `ValueError`. The output is RGB at exactly that size.
- Background: scale to cover the output, center-crop.
- Safe area: the output minus `SAFE_MARGINS[ratio]`. Use (top, right, bottom, left) margins of `(60, 60, 60, 60)` for `1:1` and `4:5`, and `(270, 65, 670, 65)` for `9:16` (Reels keep the top 14%, bottom 35% and 6% of each side clear of key content).
- Headline at the top of the safe area: white text on a dark band with some transparency, word-wrapped to the safe width, left-aligned with padding. Start at a font size of about 64 px and shrink (down to about 36 px) until the text fits in at most 3 lines; if it still does not fit, truncate the last line with "...". `text_box` is the band's bounding box.
- Product: scale the cutout keeping its aspect ratio to fit the safe area below the band (leave a 24 px gap), at most 80% of the safe width and of that remaining height, centered horizontally in it. Paste with its alpha. `product_box` is where it landed.
- Both boxes must lie fully inside the safe area and must not overlap.

### `png_bytes` and `save_png`

PNG encoding with no metadata, so the same image gives the same bytes. `save_png` creates parent directories.

## Done when

```bash
uv run pytest tests/unit/test_compose.py -q
uv run ruff check src/pgt/creative
```

Both pass. Report the exact output.
