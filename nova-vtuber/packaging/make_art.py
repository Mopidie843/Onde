"""Fabrique l'icône (nova.ico) et les images de l'installateur (wizard*.bmp) : python packaging/make_art.py
Résultats déjà fournis dans le dossier ; à relancer seulement pour changer le style."""
from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter, ImageFont

HERE = Path(__file__).resolve().parent
TOP, BOTTOM = (143, 123, 255), (255, 122, 182)        # violet Nova -> rose
BG = (15, 16, 32)


def gradient(w, h, a=TOP, b=BOTTOM, diagonal=True):
    img = Image.new("RGB", (w, h))
    px = img.load()
    for y in range(h):
        for x in range(w):
            t = ((x / max(w - 1, 1)) * 0.35 + (y / max(h - 1, 1)) * 0.65) if diagonal else y / max(h - 1, 1)
            px[x, y] = tuple(int(a[i] + (b[i] - a[i]) * t) for i in range(3))
    return img


def sparkle(draw, cx, cy, r, fill):
    """Étoile à 4 branches (le « scintillement » de Nova)."""
    k = r * 0.22
    draw.polygon([(cx, cy - r), (cx + k, cy - k), (cx + r, cy), (cx + k, cy + k),
                  (cx, cy + r), (cx - k, cy + k), (cx - r, cy), (cx - k, cy - k)], fill=fill)


def icon_image(size=1024):
    s = size
    base = gradient(s, s)
    mask = Image.new("L", (s, s), 0)
    ImageDraw.Draw(mask).rounded_rectangle((0, 0, s - 1, s - 1), radius=int(s * 0.23), fill=255)
    img = Image.new("RGBA", (s, s), (0, 0, 0, 0))
    img.paste(base, (0, 0), mask)
    glow = Image.new("RGBA", (s, s), (0, 0, 0, 0))
    gd = ImageDraw.Draw(glow)
    sparkle(gd, s * 0.5, s * 0.52, s * 0.36, (255, 255, 255, 120))
    glow = glow.filter(ImageFilter.GaussianBlur(s * 0.04))
    img = Image.alpha_composite(img, glow)
    d = ImageDraw.Draw(img)
    sparkle(d, s * 0.5, s * 0.52, s * 0.33, (255, 255, 255, 255))
    sparkle(d, s * 0.77, s * 0.24, s * 0.09, (255, 255, 255, 230))
    sparkle(d, s * 0.25, s * 0.79, s * 0.05, (255, 255, 255, 200))
    return img


def font(px):
    for name in ("segoeuib.ttf", "DejaVuSans-Bold.ttf", "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"):
        try:
            return ImageFont.truetype(name, px)
        except OSError:
            continue
    return ImageFont.load_default()


def wizard(w, h):
    img = gradient(w, h, (40, 26, 92), (120, 52, 128))
    d = ImageDraw.Draw(img, "RGBA")
    for i, (x, y, r) in enumerate([(0.2, 0.12, 0.05), (0.8, 0.2, 0.03), (0.15, 0.62, 0.025), (0.85, 0.75, 0.04),
                                   (0.6, 0.9, 0.02), (0.35, 0.33, 0.015)]):
        sparkle(d, w * x, h * y, w * r * 2, (255, 255, 255, 150 + 15 * i % 100))
    ic = icon_image(512).resize((int(w * 0.62),) * 2, Image.LANCZOS)
    img.paste(ic, ((w - ic.width) // 2, int(h * 0.30)), ic)
    f = font(int(w * 0.16))
    for text, y in (("Nova", 0.66), ("VTuber", 0.76)):
        tw = d.textlength(text, font=f)
        d.text(((w - tw) / 2, h * y), text, font=f, fill=(255, 255, 255))
    return img


def main():
    ico = icon_image(1024)
    ico.save(HERE / "nova.ico", sizes=[(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)])
    ico.resize((256, 256), Image.LANCZOS).save(HERE / "nova.png")
    for scale in (1, 2):
        wizard(164 * scale, 314 * scale).save(HERE / f"wizard{scale}.bmp")
        small = Image.new("RGB", (55 * scale, 55 * scale), (255, 255, 255))
        ic = icon_image(256).resize((55 * scale, 55 * scale), Image.LANCZOS)
        small.paste(ic, (0, 0), ic)
        small.save(HERE / f"wizard_small{scale}.bmp")
    print("Images créées dans", HERE)


if __name__ == "__main__":
    main()
