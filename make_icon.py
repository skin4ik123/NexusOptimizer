# -*- coding: utf-8 -*-
"""nexus.ico - the exe icon: a teal tile with an "N".

Called from build_installer.ps1; the application draws the same mark for
its window and the taskbar (App._set_app_icon).
"""
from PIL import Image, ImageDraw, ImageFont

ACC = (18, 164, 147, 255)
INK = (255, 255, 255, 255)


def tile(size: int) -> Image.Image:
    im = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    d = ImageDraw.Draw(im)
    rad = max(3, size * 22 // 100)
    d.rounded_rectangle([0, 0, size - 1, size - 1], radius=rad, fill=ACC)
    try:
        fnt = ImageFont.truetype("segoeuib.ttf", int(size * 0.62))
    except Exception:
        fnt = None
    d.text((size / 2, size / 2 + size * 0.02), "N", fill=INK, font=fnt,
           anchor="mm")
    return im


sizes = (16, 24, 32, 48, 64, 128, 256)
imgs = [tile(s) for s in sizes]
# every size is drawn separately so small icons stay crisp
imgs[-1].save("nexus.ico", sizes=[(s, s) for s in sizes],
              append_images=imgs[:-1])
print("nexus.ico created:", sizes)
