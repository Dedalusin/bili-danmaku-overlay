# -*- coding: utf-8 -*-
"""生成弹幕悬浮窗图标: B站蓝圆角底 + 三条滚动弹幕条 + 高亮圆点"""
from PIL import Image, ImageDraw

SIZE = 512
img = Image.new('RGBA', (SIZE, SIZE), (0, 0, 0, 0))
d = ImageDraw.Draw(img)

# 圆角蓝色渐变底 (上深下浅)
def lerp(a, b, t):
    return tuple(int(a[i] + (b[i] - a[i]) * t) for i in range(3))

top = (14, 104, 196)    # #0E68C4
bottom = (35, 173, 229)  # #23ADE5
for y in range(SIZE):
    color = lerp(top, bottom, y / SIZE)
    d.line([(0, y), (SIZE, y)], fill=color + (255,))

# 圆角遮罩
mask = Image.new('L', (SIZE, SIZE), 0)
md = ImageDraw.Draw(mask)
md.rounded_rectangle([0, 0, SIZE - 1, SIZE - 1], radius=110, fill=255)
img.putalpha(mask)

# 弹幕条: 三条白色圆角横条 (右侧圆头, 像从右滚入的弹幕)
d2 = ImageDraw.Draw(img)
# 条1 (最长, 上部)
d2.rounded_rectangle([70, 120, 420, 176], radius=28, fill=(255, 255, 255, 235))
# 条2 (中, 中部, 右侧高亮圆点装饰)
d2.rounded_rectangle([110, 216, 450, 272], radius=28, fill=(255, 255, 255, 235))
d2.ellipse([398, 232, 434, 268], fill=(255, 122, 130, 255))   # 粉色圆点
# 条3 (短, 下部)
d2.rounded_rectangle([150, 312, 400, 368], radius=28, fill=(255, 255, 255, 235))

# 保存多尺寸 ico
img.save('icon.png')
img.save('icon.ico', sizes=[(16, 16), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)])
print('icon.png / icon.ico 已生成')
