#!/usr/bin/env python3
"""Haelt alle Anzeigeseiten der BHZ als Bilder fest - als Vergleichswerte fuer
einen gleichzeitig laufenden CAN-Mitschnitt.

    snapshot.py [ZIELORDNER]      (Standard: ./snapshots)

Es entsteht ein Unterordner je Lauf; die Dateinamen enthalten die Unix-Zeit
der Aufnahme, passend zu den Zeitstempeln von "candump -L". Geklickt wird nur
auf Reiter der oberen Leiste und auf zwei Schaltflaechen, deren Seite vorher
am Bild erkannt sein muss. Am Ende steht die Anzeige wieder auf Home.
"""
import os
import sys
import time
from pathlib import Path

import rfb
import screen

BHZ_HOST = os.environ.get("BHZ_HOST", "192.168.178.20")
BHZ_PORT = int(os.environ.get("BHZ_PORT", "5900"))
SETTLE = 2.5  # Sekunden, bis die BHZ nach einem Klick neu gezeichnet hat

TITLE = (30, 170, 70, 370)  # senkrechter Seitenname am linken Rand

# Bereiche, an denen jede Seite im Vergleich mit ref/<Seite>.png erkannt wird
MARKS = {
    "kachelofen": [TITLE, (110, 375, 350, 422)],
    "kachelofen_ertraege": [TITLE, (115, 140, 200, 165)],
    "gastherme": [TITLE],
    "heizung": [TITLE, (110, 127, 350, 173)],
    "heizkreis_1": [TITLE],
    "wasser": [TITLE],
}

# Seite, Klick, Seite davor. Schaltflaechen im Seiteninhalt werden nur
# gedrueckt, wenn die Seite davor erkannt wurde, weil an derselben Stelle
# anderer Seiten Einstellungen liegen. None = Reiter der oberen Leiste.
STEPS = [
    ("kachelofen", (120, 45), None),
    ("kachelofen_ertraege", (230, 400), "kachelofen"),
    ("gastherme", (200, 45), None),
    ("heizung", (280, 45), None),
    ("heizkreis_1", (230, 150), "heizung"),
    ("wasser", (360, 45), None),
]


def capture():
    return rfb.capture(BHZ_HOST, BHZ_PORT)


def click(x, y):
    with rfb.RFB(BHZ_HOST, BHZ_PORT) as vnc:
        vnc.click(x, y)
    time.sleep(SETTLE)


def on_page(img, name):
    if name == "home":
        return screen.is_home(img)
    return all(screen._matches(img, name, box) for box in MARKS[name])


def goto(name, pos, before=None):
    """Klickt und wartet, bis die Seite erkannt ist -> Bild oder None.

    Die BHZ verschluckt gelegentlich einen Klick oder liefert noch das alte
    Bild; deshalb wird erst noch einmal gelesen, dann noch einmal geklickt -
    auf eine Schaltflaeche aber nur, solange die Seite davor noch dasteht.
    """
    for attempt in range(3):
        click(*pos)
        for _ in range(2):
            img = capture()
            if on_page(img, name):
                return img
            time.sleep(SETTLE)
        if before and not on_page(img, before):
            break
    return None


def main():
    target = Path(sys.argv[1] if len(sys.argv) > 1 else "snapshots") / time.strftime("%Y%m%d-%H%M%S")
    img = capture()
    if not screen.is_home(img):
        sys.exit("Anzeige steht nicht auf Heizung -> Home, breche ab")
    target.mkdir(parents=True)
    img.save(target / f"{time.time():.0f}_home.png")
    seen = "home"
    try:
        for name, pos, before in STEPS:
            if before and before != seen:
                print(f"{name}: Seite {before} nicht erkannt, uebersprungen")
                continue
            img = goto(name, pos, before)
            seen = name if img else None
            if img is None:
                print(f"{name}: Seite nicht erkannt, uebersprungen")
                continue
            img.save(target / f"{time.time():.0f}_{name}.png")
    finally:
        home = goto("home", screen.CLICK_HOME)
    print(target)
    return 0 if home else 1


if __name__ == "__main__":
    sys.exit(main())
