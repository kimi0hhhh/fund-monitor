# -*- coding: utf-8 -*-
"""生成应用专属图标：深蓝夜空渐变底 + 红/白上升箭头 + ¥ 符号"""
from PIL import Image, ImageDraw, ImageFont
import os

S = 512  # 主画布


def lerp(a, b, t):
    return tuple(int(a[i] + (b[i] - a[i]) * t) for i in range(3))


def make_base(size):
    # 对角线渐变：深蓝 -> 蓝紫（与应用界面同款配色）
    top, bottom = (27, 43, 85), (11, 15, 26)
    img = Image.new("RGB", (size, size))
    d = ImageDraw.Draw(img)
    for y in range(size):
        t = y / max(size - 1, 1)
        d.line([(0, y), (size, y)], fill=lerp(top, bottom, t))
    # 左上角光晕
    glow = Image.new("L", (size, size), 0)
    gd = ImageDraw.Draw(glow)
    r = int(size * 0.55)
    gd.ellipse([int(-size * 0.25), int(-size * 0.35), r, int(size * 0.25)], fill=90)
    from PIL import ImageFilter
    glow = glow.filter(ImageFilter.GaussianBlur(size * 0.12))
    img = Image.composite(Image.new("RGB", (size, size), (91, 140, 255)), img, glow)
    # 圆角遮罩
    mask = Image.new("L", (size, size), 0)
    md = ImageDraw.Draw(mask)
    md.rounded_rectangle([0, 0, size - 1, size - 1], radius=int(size * 0.22), fill=255)
    out = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    out.paste(img, (0, 0), mask)
    return out


def draw_symbol(img):
    size = img.size[0]
    d = ImageDraw.Draw(img)
    u = size / 512.0  # 单位换算

    # 上升折线（白色描边感）
    lw = int(26 * u)
    pts = [(96, 330), (196, 240), (268, 300), (416, 150)]
    d.line(pts, fill=(232, 237, 247), width=lw, joint="curve")
    for p in pts:
        d.ellipse([p[0] - lw // 2, p[1] - lw // 2,
                   p[0] + lw // 2, p[1] + lw // 2], fill=(232, 237, 247))
    # 箭头头部（红色，涨）
    ax, ay = 416, 150
    d.polygon([(ax, ay - 8), (ax + int(64 * u), ay + int(10 * u)), (ax - int(6 * u), ay + int(62 * u))],
              fill=(255, 82, 82))
    # ¥ 符号（金色）
    try:
        font = ImageFont.truetype("msyhbd.ttc", int(150 * u))
    except Exception:
        try:
            font = ImageFont.truetype("C:/Windows/Fonts/msyhbd.ttc", int(150 * u))
        except Exception:
            font = ImageFont.truetype("C:/Windows/Fonts/arialbd.ttf", int(140 * u))
    d.text((size * 0.30, size * 0.055), "¥", font=font, fill=(255, 196, 66))
    return img


base = draw_symbol(make_base(S))
ico_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "app.ico")
base.save(ico_path, sizes=[(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)])
base.convert("RGB").save(os.path.join(os.path.dirname(os.path.abspath(__file__)), "app_preview.png"))
print("saved", ico_path)
