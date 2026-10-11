#!/usr/bin/env python3
"""Lernt Zeichen aus einem Screenshot der Home-Seite, dessen Texte bekannt sind.

    learn.py BILD.png puffer_oben="61 °C" kachelofen="12,2 kW" ...

Ohne Feldangaben wird nur gezeigt, was erkannt wird.
"""
import sys

from PIL import Image

import ocr
import screen

# Zeichenfolgen, die auf dem Bildschirm ohne Luecke zusammenhaengen
LIGATURES = ["kW"]


def tokens(text, count):
    chars = [ch for ch in text if ch != " "]
    if len(chars) == count:
        return chars
    for lig in LIGATURES:
        merged = text.replace(" ", "").replace(lig, "\0")
        out = [lig if ch == "\0" else ch for ch in merged]
        if len(out) == count:
            return out
    return None


def main():
    img = Image.open(sys.argv[1]).convert("RGB")
    wanted = dict(arg.split("=", 1) for arg in sys.argv[2:])
    if not screen.is_home(img):
        sys.exit("Bild zeigt nicht die Seite Heizung -> Home")
    for name, text in wanted.items():
        glyphs = [g for _, _, g in ocr.segment(img, screen.FIELDS[name])]
        toks = tokens(text, len(glyphs))
        if toks is None:
            sys.exit(f"{name}: {len(glyphs)} Zeichen im Bild passen nicht zu {text!r}")
        new = [t for t, g in zip(toks, glyphs) if ocr.save_glyph(t, g)]
        if new:
            print(f"{name}: gelernt {new}")
    values, raw, _ = screen.read_home(img)
    for name in screen.FIELDS:
        print(f"{name:14} {raw[name]!r}")


if __name__ == "__main__":
    main()
