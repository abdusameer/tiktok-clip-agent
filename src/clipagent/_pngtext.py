"""Draw text/shape overlays to transparent PNGs. Standalone: needs only Pillow.

Runs under /usr/bin/python3 (which ships Pillow) because FFmpeg here has no text filters and the
project venv has no Pillow. Usage: python3 _pngtext.py spec.json
"""
import json
import sys

from PIL import Image, ImageDraw, ImageFilter, ImageFont

_STYLE = json.load(open(__file__.rsplit("/src/", 1)[0] + "/config/ranking_style.json"))
FONTS = {k: v for k, v in _STYLE["fonts"].items() if k in ("bold", "black")}


def wrap(draw, text, font, max_width):
    lines, cur = [], ""
    for word in text.split():
        trial = (cur + " " + word).strip()
        if cur and draw.textlength(trial, font=font) > max_width:
            lines.append(cur)
            cur = word
        else:
            cur = trial
    return lines + [cur]


def draw_text(img, b):
    font = ImageFont.truetype(FONTS[b.get("font", "bold")], b["size"])
    d = ImageDraw.Draw(img)
    lines = wrap(d, b["text"], font, b.get("max_width", img.width - 160))
    lh = int(b["size"] * b.get("line_spacing", 1.15))
    y = b["y"]
    for line in lines:
        w = d.textlength(line, font=font)
        x = b["cx"] - w / 2
        if b.get("shadow"):
            sh = Image.new("RGBA", img.size, (0, 0, 0, 0))
            ImageDraw.Draw(sh).text((x, y + b["shadow"]), line, font=font, fill=(0, 0, 0, 170),
                                    stroke_width=b.get("stroke", 0), stroke_fill=(0, 0, 0, 170))
            img.alpha_composite(sh.filter(ImageFilter.GaussianBlur(b["shadow"])))
        ImageDraw.Draw(img).text((x, y), line, font=font, fill=tuple(b.get("fill", (255, 255, 255, 255))),
                                 stroke_width=b.get("stroke", 0), stroke_fill=tuple(b.get("stroke_fill", (0, 0, 0, 255))))
        y += lh
    return len(lines)


def main(spec_path):
    for item in json.load(open(spec_path))["items"]:
        img = Image.new("RGBA", (item["w"], item["h"]), (0, 0, 0, 0))
        if item.get("pill"):                       # translucent rounded box sized to the first text block
            pl, b = item["pill"], item["blocks"][0]
            font = ImageFont.truetype(FONTS[b.get("font", "bold")], b["size"])
            w = ImageDraw.Draw(img).textlength(b["text"], font=font)
            box = [b["cx"] - w / 2 - pl["pad_x"], b["y"] - pl["pad_y"], b["cx"] + w / 2 + pl["pad_x"],
                   b["y"] + b["size"] * 1.15 + pl["pad_y"]]
            ImageDraw.Draw(img).rounded_rectangle(box, radius=pl.get("radius", 24), fill=tuple(pl["fill"]))
        for s in item.get("rects", []):
            ImageDraw.Draw(img).rounded_rectangle(s["box"], radius=s.get("radius", 12), fill=tuple(s["fill"]))
        for b in item.get("blocks", []):
            draw_text(img, b)
        img.save(item["out"])


if __name__ == "__main__":
    main(sys.argv[1])
