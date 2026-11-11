#!/usr/bin/env python3
# 生成飞牛视野（FnVista）应用图标：圆角矩形 + 山景日出，原生高分辨率渲染后 LANCZOS 下采样，消除模糊。
import math, os
from PIL import Image, ImageDraw, ImageFilter

SRC = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))  # fpk 的父目录（github-repo/...）
FPK = os.path.join(SRC, "fpk")
S = 512
R = 112  # 圆角半径

def lerp(a, b, t): return tuple(int(a[i] + (b[i] - a[i]) * t) for i in range(3))

# 天空渐变（暖色日出）
TOP = (255, 138, 92)     # #FF8A5C
MID = (255, 176, 102)    # #FFB066
BOT = (255, 217, 138)    # #FFD98A

img = Image.new("RGBA", (S, S), (0, 0, 0, 0))
px = img.load()
for y in range(S):
    t = y / (S - 1)
    if t < 0.5:
        c = lerp(TOP, MID, t / 0.5)
    else:
        c = lerp(MID, BOT, (t - 0.5) / 0.5)
    for x in range(S):
        px[x, y] = (c[0], c[1], c[2], 255)

d = ImageDraw.Draw(img)

# 太阳光晕（多层同心圆，越外越透明）
cx, cy, sr = 256, 232, 64
for i in range(7, 0, -1):
    a = int(26 * (i / 7.0))
    d.ellipse([cx - sr - i * 7, cy - sr - i * 7, cx + sr + i * 7, cy + sr + i * 7],
              fill=(255, 247, 210, a))
d.ellipse([cx - sr, cy - sr, cx + sr, cy + sr], fill=(255, 248, 224, 255))

# 远山（较浅）
def mountain(base_y, peak_x, peak_h, color):
    pts = [(0, S)]
    step = 32
    xs = list(range(0, S + 1, step))
    hs = []
    # 用几个峰构造自然起伏
    import random
    random.seed(7)
    for i, x in enumerate(xs):
        h = base_y + (peak_h if abs(x - peak_x) < 60 else 0) - random.randint(0, 26) - int(40 * math.sin(x / 70.0))
        hs.append(h)
    for x, h in zip(xs, hs):
        pts.append((x, h))
    pts.append((S, S))
    d.polygon(pts, fill=color)

mountain(330, 180, 70, (120, 84, 60, 255))   # 远山
mountain(380, 360, 90, (46, 35, 27, 255))    # 近山

# 轻微柔化让山与天空过渡更自然
img = img.filter(ImageFilter.GaussianBlur(0.6))

# 圆角矩形遮罩（四角透明）
mask = Image.new("L", (S, S), 0)
md = ImageDraw.Draw(mask)
md.rounded_rectangle([0, 0, S - 1, S - 1], radius=R, fill=255)
img.putalpha(mask)

def save(size, path):
    if size == S:
        out = img
    else:
        out = img.resize((size, size), Image.LANCZOS)
    out.save(path, "PNG", optimize=True)
    print("wrote", path, out.size)

save(256, os.path.join(FPK, "ICON.PNG"))
save(256, os.path.join(FPK, "ICON_256.PNG"))
save(64,  os.path.join(FPK, "app", "ui", "images", "icon_64.png"))
save(256, os.path.join(FPK, "app", "ui", "images", "icon_256.png"))
