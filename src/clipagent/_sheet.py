"""Contact-sheet builder; run with the interpreter that has Pillow (config fonts.renderer_python). Usage: _sheet.py spec.json

spec: {"out": png/jpg path, "cols": int, "thumb_w": int, "font": ttf path, "items": [{"file": jpg, "label": "mm:ss"}]}
"""
import json
import sys

from PIL import Image, ImageDraw, ImageFont


def main(spec_path):
    spec = json.load(open(spec_path))
    cols, tw = spec["cols"], spec["thumb_w"]
    items = spec["items"]
    font = ImageFont.truetype(spec["font"], 18)
    thumbs = []
    for it in items:
        im = Image.open(it["file"]).convert("RGB")
        im = im.resize((tw, round(im.height * tw / im.width)))
        d = ImageDraw.Draw(im)
        w = d.textlength(it["label"], font=font)
        d.rectangle([0, 0, w + 10, 24], fill=(0, 0, 0))
        d.text((5, 2), it["label"], font=font, fill=(255, 255, 255))
        thumbs.append(im)
    th = max(t.height for t in thumbs)
    rows = (len(thumbs) + cols - 1) // cols
    sheet = Image.new("RGB", (cols * tw, rows * th), (0, 0, 0))
    for i, t in enumerate(thumbs):
        sheet.paste(t, ((i % cols) * tw, (i // cols) * th))
    sheet.save(spec["out"], quality=85)


main(sys.argv[1])
