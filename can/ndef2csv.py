#!/usr/bin/env python3
"""Erzeugt aus einem Parametersatz der Brunner-Firmware (para_export.xml) die
Liste der CAN-Signale mit ihren IDs.

    ndef2csv.py para_export.xml > signale.csv

Die Parametersaetze liegen in der ausgepackten Firmware unter
/opt/brunner/parametersets/<nr>/<name>/para_export.xml; fuer die BHZ 2 ist es
0/00_EOS6_BHZ2. Aufbau der 29-Bit-ID (abgeleitet aus der Signalliste
github.com/tillsc/brunner_can, die sich damit vollstaendig aufloesen laesst):

    Bit 24-28  Art der Nachricht (0x1c = Wert vom Geraet; 0x0a/0x0b und
               0x12/0x13 = Bedienteil setzt einen Wert)
    Bit 19-23  Knoten (2 = BHZ)
    Bit 13-18  Modul
    Bit 10-12  Instanz
    Bit  8-9   0 = Wert vom Geraet, 2 = Setzen durch das Bedienteil
    Bit  0-7   Parameternummer
"""
import csv
import sys
import xml.etree.ElementTree as ET

VALUE = 0x1c


def can_id(node, module, instance, param, kind=VALUE, typ=0):
    return kind << 24 | node << 19 | module << 13 | instance << 10 | typ << 8 | param


def decode(ident):
    """-> (Art, Knoten, Modul, Instanz, Typ, Parameter)"""
    return (ident >> 24, ident >> 19 & 0x1f, ident >> 13 & 0x3f,
            ident >> 10 & 7, ident >> 8 & 3, ident & 0xff)


def label(param):
    for view in param.findall("view"):
        for tag in ("longName", "shortName"):
            if view.findtext(tag):
                return view.findtext(tag).strip()
    return ""


def signals(path):
    seen = set()
    for node in ET.parse(path).getroot().find("gecanNetwork").findall("node"):
        for module in node.findall("module"):
            for inst in module.findall("instance"):
                for param in inst.findall("parameter"):
                    key = tuple(int(e.get("id")) for e in (node, module, inst, param))
                    if key in seen:  # manche Saetze fuehren Parameter doppelt
                        continue
                    seen.add(key)
                    yield {
                        "id": f"0x{can_id(*key):08x}",
                        "knoten": f"{key[0]} {node.findtext('name')}",
                        "modul": f"{key[1]} {module.findtext('name')}",
                        "instanz": f"{key[2]} {inst.findtext('name')}",
                        "nr": key[3],
                        "name": param.findtext("name"),
                        "bezeichnung": label(param),
                        "teiler": param.findtext("factor"),
                        "einheit": param.findtext("unitSymbol"),
                        # gesetzt: das Geraet sendet den Wert von sich aus
                        "broadcast": param.findtext("broadcast") or "",
                    }


def main():
    rows = list(signals(sys.argv[1]))
    out = csv.DictWriter(sys.stdout, fieldnames=list(rows[0]), delimiter=";", lineterminator="\n")
    out.writeheader()
    out.writerows(rows)


if __name__ == "__main__":
    main()
