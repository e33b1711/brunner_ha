#!/usr/bin/env python3
"""Loest einen CAN-Mitschnitt der BHZ mit der Signalliste in Werte auf.

    candump -L can0 | candecode.py [-a] [SIGNALLISTE]
    candecode.py [-a] [SIGNALLISTE] < mitschnitt.log

Ausgegeben wird je Signal der letzte Wert; mit -a auch Signale, die 0 sind.
Aufbau der Nutzdaten (am 13.10.2026 gegen die Anzeige geprueft): 4 Byte,
der Wert steht als vorzeichenbehaftete 16-Bit-Zahl in Byte 1-2 (Big Endian).
Ein negativer Teiler in der Signalliste heisst malnehmen statt teilen.
"""
import csv
import sys
from pathlib import Path

VALUE = 0x1c
MISSING = 4000  # Rohwert eines nicht angeschlossenen Fuehlers (400,0 Grad)


def load(path):
    with open(path, newline="") as f:
        return {int(row["id"], 16): row for row in csv.DictReader(f, delimiter=";")}


def raw(data):
    """Nutzdaten -> Rohwert oder None, wenn es kein 4-Byte-Wert ist."""
    if len(data) != 4:
        return None
    return int.from_bytes(data[1:3], "big", signed=True)


def scale(value, divisor):
    divisor = int(divisor)
    if divisor < 0:
        return value * -divisor
    return value / divisor if divisor > 1 else value


def frames(lines):
    """Zeilen von "candump -L" -> (Zeit, ID, Nutzdaten)"""
    for line in lines:
        parts = line.split()
        if len(parts) != 3 or "#" not in parts[2]:
            continue
        ident, _, data = parts[2].partition("#")
        try:
            yield float(parts[0].strip("()")), int(ident, 16), bytes.fromhex(data)
        except ValueError:
            continue


def main():
    args = [a for a in sys.argv[1:] if a != "-a"]
    show_all = "-a" in sys.argv[1:]
    signals = load(args[0] if args else Path(__file__).with_name("bhz2_signale.csv"))
    last = {}
    for _, ident, data in frames(sys.stdin):
        if ident >> 24 == VALUE and raw(data) is not None:
            last[ident] = raw(data)
    for ident in sorted(last):
        value = last[ident]
        if not value and not show_all:
            continue
        sig = signals.get(ident)
        if sig is None:
            print(f"{ident:08X}  {value:>8}  (nicht in der Signalliste)")
            continue
        shown = "-" if value == MISSING and sig["einheit"] == "UNIT_CELSIUS" else f"{scale(value, sig['teiler']):g}"
        print(f"{ident:08X}  {shown:>8}  {sig['einheit']:<16} {sig['name']}  {sig['bezeichnung']}")


if __name__ == "__main__":
    main()
