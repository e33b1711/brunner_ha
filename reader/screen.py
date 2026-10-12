"""Was wo auf der Seite "Heizung -> Home" steht und wie man dorthin kommt."""
import re
from pathlib import Path

import numpy as np
from PIL import Image

import ocr

REF_DIR = Path(__file__).parent / "ref"

# Textfelder der Home-Seite: Name -> Bildausschnitt (x0, y0, x1, y1)
FIELDS = {
    "wetter": (290, 140, 360, 165),
    "heizkreis_1": (420, 140, 490, 170),
    "kachelofen": (50, 243, 150, 273),
    "gastherme": (50, 353, 150, 383),
    "puffer_oben": (250, 240, 330, 266),
    "puffer_mitte": (250, 290, 330, 316),
    "puffer_unten": (250, 340, 330, 366),
}

# Unveraenderliche Bereiche, an denen die Home-Seite erkannt wird. Ein Dialog
# oder eine andere Seite verdeckt mindestens einen davon.
HOME_MARKS = [
    (220, 140, 285, 165),  # "Wetter"
    (50, 295, 150, 312),   # "Kachelofen"
    (230, 402, 350, 424),  # "Puffer 1000 l"
    (415, 178, 495, 196),  # "Heizkreis 1"
]
BOTTOM_TABS = (240, 452, 370, 478)  # "EOS | Heizung"
HOME_ICON = (0, 0, 80, 90)
CLICK_HEIZUNG = (335, 465)
CLICK_HOME = (40, 45)

MAX_DIFF = 8.0  # mittlere Abweichung je Farbkanal (0..255)

_NUMBER = r"(-?\d+(?:,\d+)?)"


def _ref(name):
    return Image.open(REF_DIR / f"{name}.png").convert("RGB")


_REFS = {}


def _matches(img, ref_name, box):
    if ref_name not in _REFS:
        _REFS[ref_name] = _ref(ref_name)
    a = np.asarray(img.crop(box).convert("RGB")).astype(float)
    b = np.asarray(_REFS[ref_name].crop(box)).astype(float)
    return float(np.abs(a - b).mean()) <= MAX_DIFF


def is_home(img):
    return (img.size == (640, 480)
            and _matches(img, "home", BOTTOM_TABS)
            and all(_matches(img, "home", box) for box in HOME_MARKS))


def next_click_towards_home(img):
    """Naechster Klick in Richtung Home-Seite, oder None.

    Geklickt wird nur, wenn der Bildschirm eindeutig erkannt ist: unten die
    Tab-Leiste "EOS | Heizung" und (fuer den Home-Klick) oben links das
    Home-Symbol. Bei Dialogen oder unbekannten Seiten passiert nichts.
    """
    if img.size != (640, 480) or is_home(img):
        return None
    if _matches(img, "eos", BOTTOM_TABS):
        return CLICK_HEIZUNG
    if _matches(img, "heizung_other", BOTTOM_TABS) and _matches(img, "heizung_other", HOME_ICON):
        return CLICK_HOME
    return None


def _number(text, unit):
    m = re.fullmatch(_NUMBER + " " + re.escape(unit), text or "")
    return float(m.group(1).replace(",", ".")) if m else None


def read_home(img):
    """Liest alle Werte der Home-Seite.

    -> (Werte, Rohtexte, Lesungen). Nicht sicher gelesene Werte sind None.
    """
    readings = {name: ocr.read_text(img, box) for name, box in FIELDS.items()}
    raw = {name: r.text for name, r in readings.items()}
    values = {
        "aussentemperatur": _number(raw["wetter"], "°C"),
        "heizkreis_1": _number(raw["heizkreis_1"], "°C"),
        "gastherme": _number(raw["gastherme"], "°C"),
        "puffer_oben": _number(raw["puffer_oben"], "°C"),
        "puffer_mitte": _number(raw["puffer_mitte"], "°C"),
        "puffer_unten": _number(raw["puffer_unten"], "°C"),
        # Das Kachelofen-Feld zeigt im Betrieb die Leistung, sonst die Kesseltemperatur.
        "kachelofen_temperatur": _number(raw["kachelofen"], "°C"),
        "kachelofen_leistung": _number(raw["kachelofen"], "kW"),
    }
    return values, raw, readings
