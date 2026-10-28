"""Creative image composition: SAM output parsing, cutout, safe zones (task C)."""

import hashlib

import pytest
from PIL import Image

from pgt.creative.compose import (
    SAFE_MARGINS,
    Box,
    cutout,
    make_demo_product_shot,
    offline_background,
    parse_sam_output,
    png_bytes,
    render_creative,
    save_png,
)
from pgt.schemas import CREATIVE_SIZES

SAM_TEXT = (
    "<0f>0<|box;x1=497;y1=223;x2=1103;y2=1413;w=1600;h=1600|>"
    "<|mask;x=0;y=0;data=1024,522,AAECAwQF|>"
)


def inside(box, outer):
    return box[0] >= outer[0] and box[1] >= outer[1] and box[2] <= outer[2] and box[3] <= outer[3]


def overlap(a, b):
    return a[0] < b[2] and b[0] < a[2] and a[1] < b[3] and b[1] < a[3]


def safe_area(ratio):
    width, height = CREATIVE_SIZES[ratio]
    top, right, bottom, left = SAFE_MARGINS[ratio]
    return (left, top, width - right, height - bottom)


def test_parse_sam_output():
    assert parse_sam_output(SAM_TEXT) == [Box(497, 223, 1103, 1413, 1600, 1600)]
    two = SAM_TEXT + "<1f>1<|box;w=800;h=600;y1=10;x1=20;x2=40;y2=50|>"
    assert parse_sam_output(two)[1] == Box(20, 10, 40, 50, 800, 600)


@pytest.mark.parametrize(
    "text",
    [
        "no objects found",
        "<|mask;x=0;y=0;data=1,2,AA|>",
        "<|box;x1=5;y1=5;x2=5;y2=9;w=10;h=10|>",
        "<|box;x1=1;y1=1;x2=5;w=10;h=10|>",
    ],
)
def test_parse_sam_output_rejects_bad_text(text):
    with pytest.raises(ValueError):
        parse_sam_output(text)


def test_safe_margins():
    assert SAFE_MARGINS["1:1"] == (60, 60, 60, 60)
    assert SAFE_MARGINS["4:5"] == (60, 60, 60, 60)
    assert SAFE_MARGINS["9:16"] == (270, 65, 670, 65)


def test_demo_product_shot_is_deterministic_and_boxed():
    image, box = make_demo_product_shot()
    again, box_again = make_demo_product_shot()
    assert png_bytes(image) == png_bytes(again) and box == box_again
    assert image.size == (1600, 1600) and image.mode == "RGB"
    assert (box.w, box.h) == (1600, 1600)
    assert 0.33 <= (box.x2 - box.x1) / 1600 <= 0.42
    assert 0.68 <= (box.y2 - box.y1) / 1600 <= 0.77
    border = {image.getpixel((x, y)) for x in range(0, 1600, 37) for y in (0, 5, 1594, 1599)}
    assert len(border) == 1
    center = image.getpixel(((box.x1 + box.x2) // 2, (box.y1 + box.y2) // 2))
    assert center != image.getpixel((0, 0))
    small, small_box = make_demo_product_shot(800)
    assert small.size == (800, 800) and small_box.w == 800


def test_cutout_has_rounded_alpha_and_scales_the_box():
    image, box = make_demo_product_shot()
    cut = cutout(image, box)
    assert cut.mode == "RGBA"
    assert cut.size == (box.x2 - box.x1, box.y2 - box.y1)
    w, h = cut.size
    assert cut.getpixel((0, 0))[3] == 0 and cut.getpixel((w - 1, h - 1))[3] == 0
    assert cut.getpixel((w // 2, h // 2))[3] == 255
    assert cut.getpixel((w // 2, 0))[3] == 255

    half = image.resize((800, 800))
    cut_half = cutout(half, box)
    assert abs(cut_half.width - w / 2) <= 1 and abs(cut_half.height - h / 2) <= 1


def test_offline_background():
    a = offline_background("a calm sunrise over a living room")
    assert a.size == (1024, 1024) and a.mode == "RGB"
    assert png_bytes(a) == png_bytes(offline_background("a calm sunrise over a living room"))
    assert png_bytes(a) != png_bytes(offline_background("a city park at dusk"))
    assert offline_background("x", (300, 500)).size == (300, 500)


@pytest.mark.parametrize("ratio", ["1:1", "4:5", "9:16"])
def test_render_creative_respects_safe_zones(ratio):
    shot, box = make_demo_product_shot()
    product = cutout(shot, box)
    out = render_creative(offline_background("bg"), product, "Ten minutes a day adds up", ratio)
    assert out.image.size == CREATIVE_SIZES[ratio] and out.image.mode == "RGB"
    safe = safe_area(ratio)
    assert inside(out.product_box, safe) and inside(out.text_box, safe)
    assert not overlap(out.product_box, out.text_box)
    assert out.product_box[1] >= out.text_box[3] + 24
    # The product is pasted at its own center pixel, unchanged (opaque there).
    px = (out.product_box[0] + out.product_box[2]) // 2
    py = (out.product_box[1] + out.product_box[3]) // 2
    pasted = out.image.getpixel((px, py))
    scaled = product.resize(
        (out.product_box[2] - out.product_box[0], out.product_box[3] - out.product_box[1])
    ).convert("RGB")
    expected = scaled.getpixel((scaled.width // 2, scaled.height // 2))
    assert all(abs(a - b) <= 24 for a, b in zip(pasted, expected, strict=True))


def test_long_headline_wraps_and_stays_inside():
    shot, box = make_demo_product_shot()
    product = cutout(shot, box)
    short = render_creative(offline_background("bg"), product, "Move daily", "1:1")
    long = render_creative(
        offline_background("bg"),
        product,
        "A very long headline that keeps going well past the width of a single line on purpose",
        "1:1",
    )
    assert long.text_box[3] - long.text_box[1] > short.text_box[3] - short.text_box[1]
    assert inside(long.text_box, safe_area("1:1"))


def test_render_creative_rejects_unknown_ratio():
    shot, box = make_demo_product_shot(400)
    with pytest.raises(ValueError):
        render_creative(offline_background("bg"), cutout(shot, box), "Hi", "16:9")


def test_save_png_returns_file_hash(tmp_path):
    image = offline_background("bg", (64, 64))
    path = tmp_path / "nested" / "a.png"
    digest = save_png(image, path)
    assert digest == hashlib.sha256(path.read_bytes()).hexdigest()
    assert Image.open(path).size == (64, 64)
    assert path.read_bytes() == png_bytes(image)
