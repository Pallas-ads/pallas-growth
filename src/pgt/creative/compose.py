"""Deterministic product cutouts and placement images built with Pillow."""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from io import BytesIO
from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter, ImageFont, ImageOps

from pgt.schemas.weekly import CREATIVE_SIZES

SAFE_MARGINS: dict[str, tuple[int, int, int, int]] = {
    "1:1": (60, 60, 60, 60),
    "4:5": (60, 60, 60, 60),
    "9:16": (270, 65, 670, 65),
}


@dataclass(frozen=True)
class Box:
    x1: int
    y1: int
    x2: int
    y2: int
    w: int
    h: int


@dataclass(frozen=True)
class Rendered:
    image: Image.Image
    product_box: tuple[int, int, int, int]
    text_box: tuple[int, int, int, int]


def parse_sam_output(text: str) -> list[Box]:
    """Read box tokens independently of SAM's other output."""
    boxes = []
    for token in re.findall(r"<\|box;([^|]*)\|>", text):
        fields = dict(re.findall(r"(\w+)\s*=\s*([^;]+)", token))
        try:
            box = Box(*(int(fields[key]) for key in ("x1", "y1", "x2", "y2", "w", "h")))
        except (KeyError, ValueError) as exc:
            raise ValueError("Box must contain integer x1, y1, x2, y2, w and h") from exc
        if box.x2 <= box.x1 or box.y2 <= box.y1 or box.w <= 0 or box.h <= 0:
            raise ValueError("Box and reference frame must have positive dimensions")
        boxes.append(box)
    if not boxes:
        raise ValueError("No SAM box tokens found")
    return boxes


def cutout(image: Image.Image, box: Box, radius_ratio: float = 0.08) -> Image.Image:
    """Scale a detection to the image and crop it with rounded corners."""
    if box.w <= 0 or box.h <= 0:
        raise ValueError("Box reference frame must have positive dimensions")
    if not 0 <= radius_ratio <= 0.5:
        raise ValueError("Corner radius ratio must be between 0 and 0.5")
    x1, x2 = (max(0, min(image.width, round(x * image.width / box.w)))
              for x in (box.x1, box.x2))
    y1, y2 = (max(0, min(image.height, round(y * image.height / box.h)))
              for y in (box.y1, box.y2))
    if x2 <= x1 or y2 <= y1:
        raise ValueError("Box has no area inside the image")
    product = image.crop((x1, y1, x2, y2)).convert("RGBA")
    mask = Image.new("L", product.size, 0)
    ImageDraw.Draw(mask).rounded_rectangle(
        (0, 0, product.width - 1, product.height - 1),
        radius=round(radius_ratio * min(product.size)),
        fill=255,
    )
    product.putalpha(mask)
    return product


def make_demo_product_shot(size: int = 1600) -> tuple[Image.Image, Box]:
    """Draw a synthetic Pallas Fit phone on a plain product-shot background."""
    if size < 32:
        raise ValueError("Product shot size must be at least 32 pixels")
    image = Image.new("RGB", (size, size), (242, 240, 234))
    width, height = round(size * 0.38), round(size * 0.74)
    x1, y1 = (size - width) // 2, (size - height) // 2
    box = Box(x1, y1, x1 + width, y1 + height, size, size)
    draw = ImageDraw.Draw(image)
    draw.rounded_rectangle(
        (box.x1, box.y1, box.x2 - 1, box.y2 - 1),
        radius=round(width * 0.08), fill=(27, 33, 40),
    )
    inset = max(2, round(width * 0.045))
    screen = (x1 + inset, y1 + inset, box.x2 - inset - 1, box.y2 - inset - 1)
    draw.rounded_rectangle(screen, radius=round(width * 0.06), fill=(230, 240, 235))
    left = x1 + round(width * 0.12)
    draw.text(
        (left, y1 + round(height * 0.12)), "Pallas Fit",
        font=ImageFont.load_default(size=max(1, round(width * 0.12))), fill=(25, 64, 54),
    )
    draw.text(
        (left, y1 + round(height * 0.23)), "Day 12 streak",
        font=ImageFont.load_default(size=max(1, round(width * 0.065))), fill=(62, 91, 79),
    )
    bar_width, bar_height = round(width * 0.76), max(2, round(height * 0.028))
    font = ImageFont.load_default(size=max(1, round(width * 0.055)))
    for index, (label, progress) in enumerate((("Movement", 0.8), ("Strength", 0.6),
                                                ("Recovery", 0.9))):
        top = y1 + round(height * (0.39 + index * 0.15))
        draw.text((left, top), label, font=font, fill=(45, 72, 63))
        bar_top = top + round(height * 0.05)
        draw.rounded_rectangle(
            (left, bar_top, left + bar_width, bar_top + bar_height),
            radius=bar_height // 2, fill=(199, 216, 206),
        )
        draw.rounded_rectangle(
            (left, bar_top, left + round(bar_width * progress), bar_top + bar_height),
            radius=bar_height // 2, fill=(59, 130, 103),
        )
    return image, box


def offline_background(prompt: str, size: tuple[int, int] = (1024, 1024)) -> Image.Image:
    """Draw a prompt-seeded gradient with softly blurred circles."""
    width, height = size
    if width <= 0 or height <= 0:
        raise ValueError("Background dimensions must be positive")
    seed = hashlib.sha256(prompt.encode("utf-8")).digest()
    first = tuple(55 + value // 2 for value in seed[:3])
    second = tuple(55 + value // 2 for value in seed[3:6])
    image = Image.new("RGB", size)
    draw = ImageDraw.Draw(image)
    for y in range(height):
        fraction = y / max(1, height - 1)
        color = tuple(round(a + (b - a) * fraction) for a, b in zip(first, second))
        draw.line((0, y, width - 1, y), fill=color)
    circles = Image.new("RGBA", size, (0, 0, 0, 0))
    draw = ImageDraw.Draw(circles)
    for index in range(3):
        offset = 6 + index * 3
        x = round(seed[offset] / 255 * width)
        y = round(seed[offset + 1] / 255 * height)
        radius = round(min(size) * (0.12 + seed[offset + 2] / 255 * 0.15))
        draw.ellipse((x - radius, y - radius, x + radius, y + radius), fill=(255, 255, 255, 45))
    circles = circles.filter(ImageFilter.GaussianBlur(max(1, min(size) * 0.045)))
    return Image.alpha_composite(image.convert("RGBA"), circles).convert("RGB")


def _wrap_headline(text: str, font: ImageFont.FreeTypeFont, width: int) -> list[str]:
    lines: list[str] = []
    line = ""
    for word in text.split():
        candidate = f"{line} {word}" if line else word
        if font.getlength(candidate) <= width:
            line = candidate
            continue
        if line:
            lines.append(line)
            line = ""
        # Split a single word too wide for the band instead of clipping it.
        for character in word:
            if line and font.getlength(line + character) > width:
                lines.append(line)
                line = ""
            line += character
    return [*lines, line] if line or not lines else lines


def render_creative(
    background: Image.Image, product: Image.Image, headline: str, ratio: str,
) -> Rendered:
    """Compose a headline and product entirely inside the placement safe area."""
    if ratio not in CREATIVE_SIZES:
        raise ValueError(f"Unknown creative ratio: {ratio}")
    output = ImageOps.fit(
        background.convert("RGB"), CREATIVE_SIZES[ratio], method=Image.Resampling.LANCZOS,
    ).convert("RGBA")
    top, right, bottom, left = SAFE_MARGINS[ratio]
    safe_width = output.width - left - right
    safe_bottom = output.height - bottom
    padding, line_gap = 24, 8
    text_width = safe_width - 2 * padding
    for font_size in range(64, 35, -4):
        font = ImageFont.load_default(size=font_size)
        lines = _wrap_headline(headline, font, text_width)
        if len(lines) <= 3:
            break
    if len(lines) > 3:
        lines = lines[:3]
        last = lines[-1].rstrip()
        while last and font.getlength(last + "...") > text_width:
            last = last[:-1].rstrip()
        lines[-1] = last + "..."
    line_height = max(font.getbbox(line or "Ag")[3] - font.getbbox(line or "Ag")[1]
                      for line in lines)
    band_height = 2 * padding + line_height * len(lines) + line_gap * (len(lines) - 1)
    text_box = (left, top, output.width - right, top + band_height)
    band = Image.new("RGBA", (safe_width, band_height), (15, 23, 30, 205))
    draw = ImageDraw.Draw(band)
    for index, line in enumerate(lines):
        draw.text(
            (padding, padding + index * (line_height + line_gap)), line,
            font=font, fill="white", anchor="lt",
        )
    output.alpha_composite(band, (left, top))

    product_top = text_box[3] + 24
    remaining_height = safe_bottom - product_top
    scale = min(0.8 * safe_width / product.width, 0.8 * remaining_height / product.height)
    product_size = (max(1, int(product.width * scale)), max(1, int(product.height * scale)))
    resized = product.convert("RGBA").resize(product_size, Image.Resampling.LANCZOS)
    x = left + (safe_width - resized.width) // 2
    y = product_top + (remaining_height - resized.height) // 2
    product_box = (x, y, x + resized.width, y + resized.height)
    output.alpha_composite(resized, (x, y))
    return Rendered(output.convert("RGB"), product_box, text_box)


def png_bytes(image: Image.Image) -> bytes:
    """Encode pixels without inheriting source image metadata."""
    clean = Image.new(image.mode, image.size)
    clean.paste(image)
    if image.mode == "P":
        clean.putpalette(image.getpalette())
    buffer = BytesIO()
    clean.save(buffer, format="PNG")
    return buffer.getvalue()


def save_png(image: Image.Image, path: str | Path) -> str:
    """Write a deterministic PNG and return the digest of its bytes."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    data = png_bytes(image)
    path.write_bytes(data)
    return hashlib.sha256(data).hexdigest()
