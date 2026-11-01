#!/usr/bin/env python3
import math
from PIL import Image, ImageDraw

SIZE = 160
img = Image.new("RGBA", (SIZE * 4, SIZE * 4), (0, 0, 0, 0))
draw = ImageDraw.Draw(img)
S = SIZE * 4  # 640 super-sampled

# 1. 圆角深邃科技感背景 (渐变通过多层同心圆/同心圆角实现，或深灰黑背景配内发光)
# 深色渐变：深邃黑底 #0d1117 -> #161b22
draw.rounded_rectangle([0, 0, S, S], radius=140, fill=(13, 17, 23, 255))

# 科技感内发光微边框
draw.rounded_rectangle([4, 4, S - 4, S - 4], radius=136, outline=(48, 54, 61, 200), width=6)

# 2. 绘制雷达扫描波纹 (中心在偏中下方 (320, 340))
cx, cy = 320, 330

# 雷达扫描底网 (同心圆弧)
for r in [90, 160, 230]:
    draw.ellipse([cx - r, cy - r, cx + r, cy + r], outline=(30, 41, 59, 200), width=4)

# 坐标十字准星细线
draw.line([(cx - 240, cy), (cx + 240, cy)], fill=(30, 41, 59, 180), width=3)
draw.line([(cx, cy - 230), (cx, cy + 230)], fill=(30, 41, 59, 180), width=3)

# 雷达扫描扇区光晕 (淡蓝青色)
scan_poly = [(cx, cy)]
for deg in range(30, 75):
    rad = math.radians(deg)
    scan_poly.append((cx + int(225 * math.cos(rad)), cy - int(225 * math.sin(rad))))
draw.polygon(scan_poly, fill=(14, 165, 233, 40))

# 3. 绘制核心视觉：“明斯基警报脉冲折线” (预示转折点破位预警)
# 从稳定走势 -> 剧烈震荡破位 -> 警报红点
points = [
    (cx - 220, cy + 40),
    (cx - 150, cy + 30),
    (cx - 80, cy - 10),
    (cx - 10, cy - 80),   # 顶点 (资产泡沫高点)
    (cx + 60, cy + 10),   # 破位初现
    (cx + 120, cy - 20),  # 假反弹
    (cx + 190, cy + 140), # 剧烈明斯基时刻下跌
]

# 阴影发光层 (红色/橙色光晕)
for offset in range(6, 0, -2):
    glow_color = (255, 94, 98, int(35 / offset))
    draw.line(points, fill=glow_color, width=16 + offset * 4, joint="curve")

# 渐变主折线：平稳段蓝青色，下跌段亮橙红
# 分段绘制主折线
p_calm = points[:4]
p_danger = points[3:]
draw.line(p_calm, fill=(56, 189, 248, 255), width=14, joint="curve")
draw.line(p_danger, fill=(255, 94, 98, 255), width=16, joint="curve")

# 关键节点小圆点
for p in p_calm[:-1]:
    draw.ellipse([p[0] - 8, p[1] - 8, p[0] + 8, p[1] + 8], fill=(56, 189, 248, 255))

# 峰值“临界点” (黄橙色预警核心)
peak = points[3]
draw.ellipse([peak[0] - 26, peak[1] - 26, peak[0] + 26, peak[1] + 26], fill=(255, 153, 102, 60))
draw.ellipse([peak[0] - 14, peak[1] - 14, peak[0] + 14, peak[1] + 14], fill=(255, 180, 50, 255))

# 破位终点 (醒目红色告警点)
tip = points[-1]
draw.ellipse([tip[0] - 30, tip[1] - 30, tip[0] + 30, tip[1] + 30], fill=(255, 94, 98, 80))
draw.ellipse([tip[0] - 18, tip[1] - 18, tip[0] + 18, tip[1] + 18], fill=(255, 94, 98, 255))
draw.ellipse([tip[0] - 8, tip[1] - 8, tip[0] + 8, tip[1] + 8], fill=(255, 255, 255, 255))

# 4. 高质量超采样缩放至 160x160
logo_160 = img.resize((SIZE, SIZE), Image.Resampling.LANCZOS)

out_path = "/Users/xiangfangwei/.gemini/antigravity/scratch/minsky-monitor/logo_160x160.png"
logo_160.save(out_path, "PNG")
print("Saved to:", out_path)
