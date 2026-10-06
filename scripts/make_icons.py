"""Draw the app icons (shield with a check mark). Run once: python scripts/make_icons.py"""

from pathlib import Path

from PIL import Image, ImageDraw

OUT = Path(__file__).resolve().parent.parent / "payverify" / "static" / "icons"
INDIGO = (67, 56, 202, 255)
WHITE = (255, 255, 255, 255)
SCALE = 4  # draw large, then shrink, for smooth edges


def shield_points(cx, cy, size):
    """Shield outline centred on (cx, cy) with the given height."""
    w, h = size * 0.78, size
    top, left, right = cy - h / 2, cx - w / 2, cx + w / 2
    points = [(cx, top), (right, top + h * 0.14), (right, top + h * 0.48)]
    steps = 24
    for i in range(1, steps + 1):  # curved bottom (quadratic Bézier)
        t = i / steps
        p0, p1, p2 = (right, top + h * 0.48), (right, top + h * 0.82), (cx, top + h)
        points.append(((1 - t) ** 2 * p0[0] + 2 * (1 - t) * t * p1[0] + t * t * p2[0],
                       (1 - t) ** 2 * p0[1] + 2 * (1 - t) * t * p1[1] + t * t * p2[1]))
    mirrored = [(2 * cx - x, y) for x, y in reversed(points[1:-1])]
    return points + mirrored


def draw_icon(px, *, rounded, symbol_ratio):
    size = px * SCALE
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    if rounded:
        draw.rounded_rectangle((0, 0, size - 1, size - 1), radius=size * 0.22, fill=INDIGO)
    else:
        draw.rectangle((0, 0, size, size), fill=INDIGO)
    shield = size * symbol_ratio
    cx, cy = size / 2, size / 2
    draw.polygon(shield_points(cx, cy, shield), fill=WHITE)
    stroke = shield * 0.12
    check = [(cx - shield * 0.17, cy + shield * 0.01), (cx - shield * 0.04, cy + shield * 0.14),
             (cx + shield * 0.19, cy - shield * 0.12)]
    draw.line(check, fill=INDIGO, width=int(stroke), joint="curve")
    for x, y in (check[0], check[-1]):
        r = stroke / 2
        draw.ellipse((x - r, y - r, x + r, y + r), fill=INDIGO)
    return img.resize((px, px), Image.LANCZOS)


def main():
    OUT.mkdir(parents=True, exist_ok=True)
    draw_icon(192, rounded=True, symbol_ratio=0.62).save(OUT / "icon-192.png")
    draw_icon(512, rounded=True, symbol_ratio=0.62).save(OUT / "icon-512.png")
    # Maskable: full bleed, symbol inside the central safe zone.
    draw_icon(512, rounded=False, symbol_ratio=0.5).save(OUT / "icon-maskable-512.png")
    # iOS rounds the corners itself and dislikes transparency.
    draw_icon(180, rounded=False, symbol_ratio=0.6).convert("RGB").save(OUT / "apple-touch-icon.png")
    print(f"Icons written to {OUT}")


if __name__ == "__main__":
    main()
