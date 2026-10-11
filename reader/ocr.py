"""Texterkennung fuer die festen Textfelder der BHZ-Oberflaeche.

Die Oberflaeche zeichnet weissen Text immer pixelgleich. Erkannt wird deshalb
durch Vergleich mit echten, einmal vom Bildschirm gelernten Zeichen (glyphs/).
Fuer noch nie gesehene Zeichen gibt es einen Notbehelf mit aus DejaVu Sans Bold
erzeugten Zeichen, der nur bei klarem Abstand zum zweitbesten Treffer gilt.
Ist ein Zeichen nicht sicher erkennbar, gibt es kein Ergebnis statt eines
geratenen Werts.
"""
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw, ImageFont

GLYPH_DIR = Path(__file__).parent / "glyphs"
FONT = "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"
FALLBACK_CHARS = "0123456789,-"
FALLBACK_SIZE = 14

INK = 0.35             # ab dieser Deckkraft zaehlt ein Pixel als Schrift
MIN_LEARNED = 0.96     # Mindestaehnlichkeit zu einem gelernten Zeichen ...
LEARNED_MARGIN = 0.02  # ... und Mindestabstand zum aehnlichsten anderen Zeichen
MIN_FALLBACK = 0.85    # Mindestaehnlichkeit zu einem erzeugten Zeichen ...
MIN_MARGIN = 0.05      # ... und Mindestabstand zum zweitbesten
WORD_GAP = 4           # ab so vielen leeren Spalten beginnt ein neues Wort


@dataclass
class Reading:
    text: str | None                 # None: mindestens ein Zeichen unbekannt
    glyphs: list = field(default_factory=list)    # alle Zeichenbilder, von links
    unknown: list = field(default_factory=list)   # nicht erkannte Zeichenbilder
    guessed: list = field(default_factory=list)   # (Zeichen, Bild) aus dem Notbehelf


def alpha(img, box):
    """Deckkraft des weissen Texts (0..1) im Ausschnitt box=(x0, y0, x1, y1).

    Der Hintergrund ist ein senkrechter Verlauf, also je Zeile konstant; als
    Hintergrundfarbe dient der Median der Zeile.
    """
    c = np.asarray(img.crop(box).convert("RGB")).astype(float)
    bg = np.median(c, axis=1, keepdims=True)
    # Kleinste-Quadrate-Schaetzung ueber die Farbkanaele: Kanaele, in denen der
    # Hintergrund dunkel ist, tragen am meisten bei (wichtig auf Rot und Blau).
    span = 255.0 - bg
    return np.clip(((c - bg) * span).sum(axis=2) / ((span * span).sum(axis=2) + 1e-6), 0, 1)


def _pad(glyph):
    return np.pad(glyph, 1)


def _subpixel_shifts(glyph):
    """Das Zeichen um 0, 1/4, 1/2 und 3/4 Pixel nach rechts verschoben."""
    out = []
    for f in (0.0, 0.25, 0.5, 0.75):
        shifted = np.zeros((glyph.shape[0], glyph.shape[1] + 1))
        shifted[:, :-1] += (1 - f) * glyph
        shifted[:, 1:] += f * glyph
        out.append(shifted)
    return out


def _correlation(a, b):
    """Beste normierte Korrelation ueber ganzzahlige Verschiebungen."""
    h, w = max(a.shape[0], b.shape[0]) + 1, max(a.shape[1], b.shape[1]) + 2
    pb = np.zeros((h, w))
    pb[:b.shape[0], :b.shape[1]] = b
    nb = np.sqrt((pb * pb).sum())
    best = 0.0
    for dy in range(h - a.shape[0] + 1):
        for dx in range(w - a.shape[1] + 1):
            pa = np.zeros((h, w))
            pa[dy:dy + a.shape[0], dx:dx + a.shape[1]] = a
            na = np.sqrt((pa * pa).sum())
            best = max(best, float((pa * pb).sum() / (na * nb + 1e-9)))
    return best


def _similarity(a, b):
    """Aehnlichkeit zweier Zeichenbilder (0..1).

    Die BHZ setzt Zeichen auf Bruchteile eines Pixels genau, dasselbe Zeichen
    sieht also je nach Position leicht anders aus. Deshalb wird auch gegen
    waagrecht um Pixelbruchteile verschobene Fassungen verglichen.
    """
    if abs(a.shape[0] - b.shape[0]) > 1 or abs(a.shape[1] - b.shape[1]) > 2:
        return 0.0
    return max(max(_correlation(a, vb) for vb in _subpixel_shifts(b)),
               max(_correlation(va, b) for va in _subpixel_shifts(a)))


def _label_to_name(label):
    return "-".join(f"u{ord(ch):04x}" for ch in label)


def _name_to_label(name):
    return "".join(chr(int(part[1:], 16)) for part in name.split("-"))


@lru_cache(maxsize=1)
def learned():
    """Gelernte Zeichen: Liste aus (Text, Bild). Dateiname: <codepoints>_<nr>.png"""
    out = []
    for path in sorted(GLYPH_DIR.glob("*.png")):
        label = _name_to_label(path.stem.rsplit("_", 1)[0])
        out.append((label, np.asarray(Image.open(path).convert("L")).astype(float) / 255.0))
    return out


def save_glyph(label, glyph):
    """Speichert ein Zeichenbild als gelerntes Zeichen (falls noch nicht vorhanden)."""
    if any(lab == label and _similarity(glyph, g) > 0.985 for lab, g in learned()):
        return False
    GLYPH_DIR.mkdir(exist_ok=True)
    n = 1
    while (path := GLYPH_DIR / f"{_label_to_name(label)}_{n}.png").exists():
        n += 1
    Image.fromarray((glyph * 255).round().astype(np.uint8)).save(path)
    learned.cache_clear()
    return True


@lru_cache(maxsize=1)
def _fallback():
    font = ImageFont.truetype(FONT, FALLBACK_SIZE)
    out = []
    for ch in FALLBACK_CHARS:
        img = Image.new("L", (40, 40), 0)
        ImageDraw.Draw(img).text((10, 10), ch, font=font, fill=255)
        a = np.asarray(img).astype(float) / 255.0
        ys = np.where((a > INK).any(axis=1))[0]
        xs = np.where((a > INK).any(axis=0))[0]
        out.append((ch, _pad(a[ys[0]:ys[-1] + 1, xs[0]:xs[-1] + 1])))
    return out


def _classify(glyph):
    """-> (Text, 'learned' | 'guessed') oder (None, None)"""
    scores = sorted(((_similarity(glyph, g), lab) for lab, g in learned()), reverse=True)
    if scores and scores[0][0] >= MIN_LEARNED:
        label = scores[0][1]
        other = max((s for s, lab in scores if lab != label), default=0.0)
        if scores[0][0] - other >= LEARNED_MARGIN:
            return label, "learned"
    try:
        scores = sorted(((_similarity(glyph, g), ch) for ch, g in _fallback()), reverse=True)
    except OSError:  # Schrift nicht installiert
        return None, None
    if scores[0][0] >= MIN_FALLBACK and scores[0][0] - scores[1][0] >= MIN_MARGIN:
        return scores[0][1], "guessed"
    return None, None


def segment(img, box):
    """Zerlegt den Text im Ausschnitt in Zeichenbilder: Liste aus (x0, x1, Bild).

    Jedes Bild behaelt einen Pixel Rand, damit die schwachen Randpixel der
    Kantenglaettung erhalten bleiben.
    """
    a = np.pad(alpha(img, box), 1)
    cols = list((a > INK).any(axis=0)) + [False]
    out, start = [], None
    for x, on in enumerate(cols):
        if on and start is None:
            start = x
        elif not on and start is not None:
            ys = np.where((a[:, start:x] > INK).any(axis=1))[0]
            out.append((start, x, a[ys[0] - 1:ys[-1] + 2, start - 1:x + 1]))
            start = None
    return out


def read_text(img, box):
    result = Reading(text="")
    prev_end = None
    for x0, x1, glyph in segment(img, box):
        result.glyphs.append(glyph)
        label, how = _classify(glyph)
        if label is None:
            result.unknown.append(glyph)
        elif how == "guessed":
            result.guessed.append((label, glyph))
        if result.text is not None:
            if label is None:
                result.text = None
            else:
                if prev_end is not None and x0 - prev_end >= WORD_GAP:
                    result.text += " "
                result.text += label
        prev_end = x1
    if not result.glyphs:
        result.text = None
    return result
