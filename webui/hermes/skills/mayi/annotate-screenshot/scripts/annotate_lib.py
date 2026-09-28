"""Stable Pillow helpers for the rerun docs/release annotation style."""

from __future__ import annotations

import os
from pathlib import Path
from typing import BinaryIO, Iterable, Sequence

from PIL import Image, ImageDraw, ImageFont, ImageOps


RED = (229, 57, 70)
RED_OVERLAY = (*RED, 36)
LEGEND_BG = (47, 47, 47, 235)
WHITE = (255, 255, 255, 255)


def _font_candidates(bold: bool = False) -> list[str]:
    configured = os.environ.get("ANNOTATE_FONT", "").strip()
    names = [
        configured,
        "/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc" if bold else "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
        "/usr/share/fonts/truetype/wqy/wqy-zenhei.ttc",
        "/System/Library/Fonts/STHeiti Medium.ttc" if bold else "/System/Library/Fonts/STHeiti Light.ttc",
        "/System/Library/Fonts/Supplemental/Arial Unicode.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf" if bold else "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    ]
    return [name for name in names if name and Path(name).is_file()]


def _load_font(size: int, *, bold: bool = False) -> ImageFont.ImageFont:
    for candidate in _font_candidates(bold):
        try:
            return ImageFont.truetype(candidate, size=size)
        except OSError:
            continue
    return ImageFont.load_default(size=max(10, size))


def _as_xy(value: Sequence[float]) -> tuple[int, int]:
    if len(value) != 2:
        raise ValueError("point must contain exactly two coordinates")
    return round(float(value[0])), round(float(value[1]))


def _as_box(value: Sequence[float]) -> tuple[int, int, int, int]:
    if len(value) != 4:
        raise ValueError("box must contain exactly four coordinates")
    x0, y0, x1, y1 = (round(float(v)) for v in value)
    return min(x0, x1), min(y0, y1), max(x0, x1), max(y0, y1)


class Annotator:
    """Draw the established red annotation style on one image."""

    def __init__(self, source: str | os.PathLike | BinaryIO, scale: float = 1):
        if not 0.25 <= float(scale) <= 8:
            raise ValueError("scale must be between 0.25 and 8")
        self.scale = float(scale)
        with Image.open(source) as opened:
            raw = ImageOps.exif_transpose(opened)
            self.raw = raw.convert("RGBA")
        self.image = self.raw.copy()
        self.draw = ImageDraw.Draw(self.image, "RGBA")

    def _px(self, value: float) -> int:
        return max(1, round(value * self.scale))

    def _font(self, value: float, *, bold: bool = False) -> ImageFont.ImageFont:
        return _load_font(self._px(value), bold=bold)

    def box(self, bounds: Sequence[float]) -> "Annotator":
        """Small target: translucent fill plus solid red outline."""
        xy = _as_box(bounds)
        self.draw.rectangle(xy, fill=RED_OVERLAY, outline=(*RED, 255), width=self._px(3))
        return self

    def rect(self, bounds: Sequence[float]) -> "Annotator":
        """Large region: outline only, without a red veil."""
        self.draw.rectangle(_as_box(bounds), outline=(*RED, 255), width=self._px(3))
        return self

    def line(self, points: Iterable[Sequence[float]], width: float = 4) -> "Annotator":
        """Trace one visible crease or contour with the standard red stroke."""
        xy = [_as_xy(point) for point in points]
        if len(xy) < 2:
            raise ValueError("line requires at least two points")
        self.draw.line(xy, fill=(*RED, 255), width=self._px(width), joint="curve")
        radius = self._px(width * 0.7)
        for x, y in (xy[0], xy[-1]):
            self.draw.ellipse((x - radius, y - radius, x + radius, y + radius), fill=(*RED, 255))
        return self

    polyline = line

    def circle(self, position: Sequence[float], number: int | str) -> "Annotator":
        center = _as_xy(position)
        radius = self._px(14)
        x, y = center
        self.draw.ellipse(
            (x - radius, y - radius, x + radius, y + radius),
            fill=(*RED, 255),
            outline=WHITE,
            width=self._px(1.5),
        )
        label = str(number)
        font = self._font(16, bold=True)
        bbox = self.draw.textbbox((0, 0), label, font=font, stroke_width=0)
        tw, th = bbox[2] - bbox[0], bbox[3] - bbox[1]
        self.draw.text((x - tw / 2, y - th / 2 - bbox[1]), label, font=font, fill=WHITE)
        return self

    def chip(self, position: Sequence[float], text: str) -> "Annotator":
        x, y = _as_xy(position)
        font = self._font(17, bold=True)
        padding_x, padding_y = self._px(11), self._px(7)
        bbox = self.draw.textbbox((0, 0), str(text), font=font)
        width = bbox[2] - bbox[0] + padding_x * 2
        height = bbox[3] - bbox[1] + padding_y * 2
        x = min(max(0, x), max(0, self.image.width - width))
        y = min(max(0, y), max(0, self.image.height - height))
        self.draw.rounded_rectangle(
            (x, y, x + width, y + height),
            radius=self._px(5),
            fill=LEGEND_BG,
            outline=(*RED, 255),
            width=self._px(2),
        )
        self.draw.text((x + padding_x, y + padding_y - bbox[1]), str(text), font=font, fill=WHITE)
        return self

    def _wrap(self, text: str, font: ImageFont.ImageFont, max_width: int) -> list[str]:
        text = str(text)
        if self.draw.textlength(text, font=font) <= max_width:
            return [text]
        lines: list[str] = []
        current = ""
        for char in text:
            candidate = current + char
            if current and self.draw.textlength(candidate, font=font) > max_width:
                lines.append(current.rstrip())
                current = char.lstrip()
            else:
                current = candidate
        if current or not lines:
            lines.append(current.rstrip())
        return lines

    def legend(
        self,
        position: Sequence[float],
        title: str,
        rows: Iterable[tuple[int | str | None, str]],
        *,
        max_width: int | None = None,
    ) -> "Annotator":
        """Render a dark, red-bordered legend and keep it inside the image."""
        x, y = _as_xy(position)
        pad = self._px(14)
        gap = self._px(8)
        badge_diameter = self._px(20)
        title_font = self._font(18, bold=True)
        body_font = self._font(16)
        available = max(80, self.image.width - max(0, x) - pad * 2)
        content_limit = min(max_width or available, available)
        text_limit = max(40, content_limit - badge_diameter - gap)

        expanded: list[tuple[int | str | None, str]] = []
        for number, text in rows:
            wrapped = self._wrap(str(text), body_font, text_limit)
            expanded.append((number, wrapped[0]))
            expanded.extend((None, "  " + line) for line in wrapped[1:])

        title_box = self.draw.textbbox((0, 0), str(title), font=title_font)
        title_height = title_box[3] - title_box[1]
        line_height = max(badge_diameter, self._px(20))
        widest = self.draw.textlength(str(title), font=title_font)
        for number, text in expanded:
            prefix = badge_diameter + gap if number is not None else 0
            widest = max(widest, prefix + self.draw.textlength(text, font=body_font))
        width = min(self.image.width, round(widest) + pad * 2)
        height = pad + title_height + gap + len(expanded) * (line_height + self._px(3)) + pad
        x = min(max(0, x), max(0, self.image.width - width))
        y = min(max(0, y), max(0, self.image.height - height))

        self.draw.rounded_rectangle(
            (x, y, x + width, y + height),
            radius=self._px(7),
            fill=LEGEND_BG,
            outline=(*RED, 255),
            width=self._px(2),
        )
        self.draw.text((x + pad, y + pad - title_box[1]), str(title), font=title_font, fill=WHITE)
        cursor_y = y + pad + title_height + gap
        for number, text in expanded:
            text_x = x + pad
            if number is not None:
                radius = badge_diameter // 2
                cx, cy = text_x + radius, cursor_y + line_height // 2
                self.draw.ellipse((cx - radius, cy - radius, cx + radius, cy + radius), fill=(*RED, 255))
                label = str(number)
                number_font = self._font(12, bold=True)
                nb = self.draw.textbbox((0, 0), label, font=number_font)
                self.draw.text(
                    (cx - (nb[2] - nb[0]) / 2, cy - (nb[3] - nb[1]) / 2 - nb[1]),
                    label,
                    font=number_font,
                    fill=WHITE,
                )
                text_x += badge_diameter + gap
            tb = self.draw.textbbox((0, 0), text, font=body_font)
            self.draw.text((text_x, cursor_y + (line_height - (tb[3] - tb[1])) / 2 - tb[1]), text, font=body_font, fill=WHITE)
            cursor_y += line_height + self._px(3)
        return self

    @staticmethod
    def _save_image(image: Image.Image, output: str | os.PathLike) -> Path:
        path = Path(output)
        path.parent.mkdir(parents=True, exist_ok=True)
        suffix = path.suffix.lower()
        if suffix in {".jpg", ".jpeg"}:
            image.convert("RGB").save(path, format="JPEG", quality=94, optimize=True)
        else:
            image.save(path, format="PNG", optimize=True)
        return path

    def save(self, output: str | os.PathLike) -> Path:
        return self._save_image(self.image, output)

    def save_raw(self, output: str | os.PathLike) -> Path:
        return self._save_image(self.raw, output)


def stitch_2x_tiles(
    tiles: Sequence[str | os.PathLike | Image.Image],
    output: str | os.PathLike | None = None,
    *,
    overlap: int | Sequence[int] = 0,
) -> Image.Image:
    """Stitch TL, TR, BL, BR Chrome 2x tiles into one full-page image."""
    if len(tiles) != 4:
        raise ValueError("stitch_2x_tiles requires four tiles: TL, TR, BL, BR")
    opened: list[Image.Image] = []
    for tile in tiles:
        if isinstance(tile, Image.Image):
            opened.append(tile.convert("RGBA"))
        else:
            with Image.open(tile) as image:
                opened.append(ImageOps.exif_transpose(image).convert("RGBA"))

    if isinstance(overlap, Sequence) and not isinstance(overlap, (str, bytes)):
        if len(overlap) != 2:
            raise ValueError("overlap must be one integer or (horizontal, vertical)")
        overlap_x, overlap_y = int(overlap[0]), int(overlap[1])
    else:
        overlap_x = overlap_y = int(overlap)
    if overlap_x < 0 or overlap_y < 0:
        raise ValueError("overlap cannot be negative")

    tl, tr, bl, br = opened
    left_width = max(tl.width, bl.width)
    top_height = max(tl.height, tr.height)
    right_x = max(0, left_width - overlap_x)
    bottom_y = max(0, top_height - overlap_y)
    canvas = Image.new(
        "RGBA",
        (max(left_width, right_x + tr.width, right_x + br.width),
         max(top_height, bottom_y + bl.height, bottom_y + br.height)),
        (255, 255, 255, 255),
    )
    canvas.alpha_composite(tl, (0, 0))
    canvas.alpha_composite(tr, (right_x, 0))
    canvas.alpha_composite(bl, (0, bottom_y))
    canvas.alpha_composite(br, (right_x, bottom_y))
    if output is not None:
        Annotator._save_image(canvas, output)
    return canvas
