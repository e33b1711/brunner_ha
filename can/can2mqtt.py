#!/usr/bin/env python3
"""Hoert den CAN-Bus der Brunner BHZ mit und meldet die Werte per MQTT
(Home Assistant MQTT Discovery). BHZ und EOS schicken alle Werte von selbst.
Gesendet wird ausschliesslich fuer SWITCHES, SELECTS und NUMBERS, und zwar genau
die Nachricht, die das Bedienteil beim Umschalten schickt.

Gemeldet wird alles aus der Signalliste (bhz2_signale.csv), was auf dem Bus
tatsaechlich vorkommt - ausser Anlagenteilen, die es hier nicht gibt (EXCLUDE),
PINs und nicht angeschlossenen Fuehlern. In Home Assistant entstehen zwei
Geraete, eines fuer die BHZ und eines fuer die EOS. Ein Stern am Namen heisst:
Zuordnung oder Bedeutung ist noch nicht an der Anzeige geprueft (VALIDATED).

Einstellungen ueber Umgebungsvariablen:
    CAN_IF                      CAN-Schnittstelle (Standard can0)
    MQTT_HOST, MQTT_PORT        Broker
    MQTT_USER, MQTT_PASSWORD    optional
    MIN_INTERVAL                fruehestens so viele Sekunden nach der letzten
                                Meldung wird eine Aenderung gemeldet (Standard 5)
    MAX_INTERVAL                ohne Aenderung wird so oft gemeldet (Standard 60)
    --once                      10 s mithoeren, Werte ausgeben, nichts senden
"""
import csv
import json
import logging
import os
import re
import socket
import struct
import sys
import time
from collections import Counter, namedtuple
from pathlib import Path

CAN_IF = os.environ.get("CAN_IF", "can0")
MQTT_HOST = os.environ.get("MQTT_HOST", "localhost")
MQTT_PORT = int(os.environ.get("MQTT_PORT", "1883"))
MQTT_USER = os.environ.get("MQTT_USER")
MQTT_PASSWORD = os.environ.get("MQTT_PASSWORD")
MIN_INTERVAL = float(os.environ.get("MIN_INTERVAL", "5"))
MAX_INTERVAL = float(os.environ.get("MAX_INTERVAL", "60"))

SIGNALS = Path(__file__).with_name("bhz2_signale.csv")
BASE = "brunner/can"
AVAIL_TOPIC = f"{BASE}/available"

# Knoten -> (Kurzname, Geraet in Home Assistant)
DEVICES = {
    2: ("bhz", {"identifiers": ["brunner_bhz_can"], "name": "Brunner BHZ CAN",
                "manufacturer": "Brunner", "model": "BHZ 2"}),
    3: ("eos", {"identifiers": ["brunner_eos_can"], "name": "Brunner EOS CAN",
                "manufacturer": "Brunner", "model": "EOS"}),
}

LISTEN_FIRST = 10    # Sekunden mithoeren vor der ersten Meldung
STALE_AFTER = 120    # Sekunden; die meisten Werte kommen alle paar Sekunden,
                     # manche lassen aber auch einmal 35 s auf sich warten
MISSING_FROM = 4000  # Rohwert ab 400,0 Grad = Fuehler nicht angeschlossen ...
BHZ_PROBES = range(0x1C104006, 0x1C104018)  # ... bei den Fuehlern S1-S18 der BHZ

FRAME = struct.Struct("=IB3x8s")  # struct can_frame
ID_MASK = 0x1FFFFFFF
EFF_FLAG = 0x80000000  # 29-Bit-ID

Switch = namedtuple("Switch", "name set_id state_id on off")

# Schalter: Schluessel -> (Anzeigename, ID zum Setzen, ID des gemeldeten Werts,
# Wert fuer Ein, Wert fuer Aus). Mitgeschnitten am 13.10.2026, als der Nutzer
# am Bedienteil schaltete: 0B112224#000001 = Heizung aus, #000000 = wieder ein;
# die BHZ antwortet mit 0A112224 und meldet danach den neuen Wert.
SWITCHES = {
    "heizkreis_1_schalter": Switch("Heizkreis 1 Ein/Aus", 0x0B112224, 0x1C112024, on=0, off=1),
    # ebenso mitgeschnitten, als die Schaltflaeche per VNC gedrueckt wurde
    "gastherme_schalter": Switch("Gastherme Ein/Aus", 0x0B10A614, 0x1C10A414, on=1, off=0),
}

Select = namedtuple("Select", "name set_id state_id")

# Auswahlfelder: Schluessel -> (Anzeigename, ID zum Setzen, ID des gemeldeten
# Werts); die Auswahl sind die Texte aus TEXTS. Mitgeschnitten am 13.10.2026,
# als der Nutzer am Bedienteil alle fuenf Einstellungen durchschaltete
# (0B10A612#00000x, auch als 1310A612 gesehen).
SELECTS = {
    "gastherme_aktiviert_fuer_auswahl": Select("Gastherme aktiviert für einstellen", 0x0B10A612, 0x1C10A412),
}

Number = namedtuple("Number", "name set_id state_id low high step unit")

# Zahlenwerte: Schluessel -> (Anzeigename, ID zum Setzen, ID des gemeldeten
# Werts, kleinster und groesster Wert, Schrittweite, Einheit). Grenzen und
# Schrittweite von Waermer/Kaelter sind am Schieberegler der Anzeige abgelesen
# (Schritt 1 Grad; die Grenzen +-10 nur aus seiner Laenge geschaetzt).
NUMBERS = {
    "heizkreis_1_waermer_kaelter": Number("Heizkreis 1 Wärmer/Kälter einstellen",
                                          0x0B112225, 0x1C112025, -10, 10, 1, "°C"),
}
COMMAND_TOPIC = f"{BASE}/set/"

# (Knoten, Modul, Instanz) -> (Vorsatz fuer den Schluessel, Vorsatz fuer den Namen)
AREAS = {
    (2, "1 System", "0 System"): ("system_", "System "),
    (2, "2 Sensor", "0 Sensor"): ("", ""),
    (2, "3 Aktor", "0 Aktor"): ("", ""),
    (2, "4 Kachelofen", "0 Kachelofen"): ("kachelofen_", "Kachelofen "),
    (2, "5 Pellet", "1 ZH1"): ("gastherme_", "Gastherme "),
    (2, "6 Warmwasser", "0 Warmwasser"): ("warmwasser_", "Warmwasser "),
    (2, "7 Zirkulation", "0 Zirkulation"): ("zirkulation_", "Zirkulation "),
    (2, "9 HK1", "0 HK1"): ("heizkreis_1_", "Heizkreis 1 "),
    (3, "1 System", "0 System"): ("eos_system_", "System "),
}
EOS_AREA = ("eos_", "")

# Gibt es an dieser Anlage nicht: Solar, Heizkreis 2, Pellet, Zusatzheizung 2
EXCLUDE_AREAS = {
    (2, "8 Solar", "0 Solar"), (2, "9 HK1", "1 HK2"), (2, "5 Pellet", "0 Pellet"),
    (2, "5 Pellet", "2 ZH2"), (3, "8 Pellet", "0 Pellet"),
}
EXCLUDE_NAMES = {
    "BHZB30_SENSOR_S2", "BHZB30_SENSOR_S6", "BHZB30_SENSOR_S7", "BHZB30_SENSOR_S17",
    "BHZB30_AKTOR_A12", "BHZB30_AKTOR_A13", "BHZB30_AKTOR_A14", "BHZB30_HK2_SOLL",
    "BHZB30_AKTOR_A16", "BHZB30_AKTOR_A19", "BHZB30_AKTOR_A21", "BHZB30_RS485O",
    "BHZB30_ZUSATZ_PELLETICON", "EOS_SERV_PELLET_SW",
}
EXCLUDE_UNITS = {"PINCODE"}

# Einheit der Signalliste -> (Einheit, device_class, state_class)
UNITS = {
    "UNIT_CELSIUS": ("°C", "temperature", "measurement"),
    "DELTA_CELSIUS": ("K", None, "measurement"),
    "KW": ("kW", "power", "measurement"),
    "KWH": ("kWh", "energy", None),
    "UNIT_PROZENT": ("%", None, "measurement"),
    "LMIN": ("L/min", None, "measurement"),
    "UNIT_LITER": ("L", None, None),
    "UNIT_SEKUNDEN": ("s", None, None),
    "UNIT_MINUTEN": ("min", None, None),
    "UNIT_STUNDEN": ("h", None, None),
    "TAGE": ("d", None, None),
}
BINARY_UNITS = {"EINAUS"}
TEXT_UNITS = {"UNIT_AUSWAHL", "STATUS", "ZUSTAND"}

# Vorzeichenlose Zaehler aus zwei Signalen: ID des LO-Teils -> ID des HI-Teils
PAIRS = {0x1C108022: 0x1C108023, 0x1C10801B: 0x1C10801C}
# Die Anzeige zaehlt bei diesen Ertraegen den heutigen Tag dazu
TODAY = 0x1C10801D
ADD_TODAY = {0x1C10801F, 0x1C108020, 0x1C108021}
TOTAL_INCREASING = {0x1C10801D, 0x1C108022, 0x1C10801B}
# Teiler, wo die Signalliste nicht zur Anzeige passt
DIVISORS = {0x1C108022: 10}

# Eigene Schluessel und Namen; alles andere wird aus der Signalliste gebildet
NAMES = {
    0x1C104008: ("puffer_oben", "Puffer oben"),
    0x1C104009: ("puffer_mitte", "Puffer Mitte"),
    0x1C10400A: ("puffer_unten", "Puffer unten"),
    0x1C104010: ("aussentemperatur", "Außentemperatur"),
    0x1C104006: ("heizkreis_1", "Heizkreis 1"),
    0x1C106017: ("heizkreis_1_soll", "Heizkreis 1 Vorlauf Soll"),
    0x1C106008: ("heizkreis_1_pumpe", "Heizkreis 1 Pumpe"),
    0x1C106009: ("heizkreis_1_mischer_auf", "Heizkreis 1 Mischer auf"),
    0x1C10600A: ("heizkreis_1_mischer_zu", "Heizkreis 1 Mischer zu"),
    0x1C112023: ("heizkreis_1_programm", "Heizkreis 1 Programm"),
    0x1C104014: ("gastherme", "Gastherme"),
    0x1C10A402: ("gastherme_status", "Gastherme Status"),
    0x1C10A412: ("gastherme_aktivierungsstatus", "Gastherme aktiviert für"),
    0x1C10A414: ("gastherme_zusatz_aktiv", "Gastherme Ein"),
    0x1C10601A: ("gastherme_anforderung", "Gastherme Anforderung (AKTOR_ZH1)"),
    0x1C104011: ("kachelofen_temperatur", "Kachelofen Temperatur"),
    0x1C186000: ("kachelofen_brennraum", "Brennraum"),
    0x1C186001: ("eos_kessel", "Kessel"),
    0x1C10801A: ("kachelofen_leistung", "Kachelofen Leistung"),
    0x1C188004: ("kachelofen_luftklappe", "Luftklappe K1 Soll"),
    0x1C106000: ("kachelofen_primaerpumpe", "Kachelofen Primärpumpe"),
    0x1C106001: ("kachelofen_sekundaerpumpe", "Kachelofen Sekundärpumpe"),
    0x1C10801D: ("kachelofen_ertrag_heute", "Kachelofen Ertrag heute"),
    0x1C10801E: ("kachelofen_ertrag_gestern", "Kachelofen Ertrag gestern"),
    0x1C10801F: ("kachelofen_ertrag_5_tage", "Kachelofen Ertrag letzte 5 Tage"),
    0x1C108020: ("kachelofen_ertrag_4_wochen", "Kachelofen Ertrag letzte 4 Wochen"),
    0x1C108021: ("kachelofen_ertrag_6_monate", "Kachelofen Ertrag letzte 6 Monate"),
    0x1C108022: ("kachelofen_ertrag_jahr", "Kachelofen Ertrag Jahr"),
    0x1C10801B: ("kachelofen_waermezaehler", "Kachelofen Wärmezähler"),
    0x1C108024: ("kachelofen_ertrag_vorjahr_1", "Kachelofen Ertrag Vorjahr 1"),
    0x1C108025: ("kachelofen_ertrag_vorjahr_2", "Kachelofen Ertrag Vorjahr 2"),
    0x1C108026: ("kachelofen_ertrag_vorjahr_3", "Kachelofen Ertrag Vorjahr 3"),
    0x1C108027: ("kachelofen_ertrag_vorjahr_4", "Kachelofen Ertrag Vorjahr 4"),
    0x1C108028: ("kachelofen_ertrag_vorjahr_5", "Kachelofen Ertrag Vorjahr 5"),
    0x1C10400F: ("fuehler_s10", "Fühler S10"),
    0x1C104012: ("fuehler_s13", "Fühler S13 Rücklauf"),
}

# Am 13.10.2026 mit der Anzeige verglichen und fuer richtig befunden. Werte,
# die dabei 0 oder "Aus" waren, zaehlen nicht - das beweist die Zuordnung nicht.
VALIDATED = {
    0x1C104008, 0x1C104009, 0x1C10400A, 0x1C104010, 0x1C104006, 0x1C104014, 0x1C104011,
    0x1C106008, 0x1C112023, 0x1C112014, 0x1C112025, 0x1C11200F, 0x1C10A402, 0x1C10A413,
    0x1C186001, 0x1C10801D, 0x1C10801E, 0x1C10801F, 0x1C108020, 0x1C108021, 0x1C108022,
    0x1C108024, 0x1C108025, 0x1C108026, 0x1C108027,
    0x1C108029, 0x1C10802A, 0x1C10802B, 0x1C10802C, 0x1C10802D,
    0x1C112024,  # Heizkreis 1 Heizung aus: vom Nutzer an der Anlage geschaltet
    0x1C10A412,  # Gastherme aktiviert fuer: vom Nutzer alle Werte durchgeschaltet
    0x1C10A414,  # Gastherme Ein: per VNC an der Anzeige geschaltet
}

# Bedeutung der Rohwerte, soweit an der Anzeige abgelesen; alles andere wird
# als Zahl gemeldet.
TEXTS = {
    0x1C112023: {3: "custom1"},
    0x1C10A413: {3: "custom1"},
    0x1C10A402: {4: "Aus", 5: "Bereit"},
    0x1C10A412: {0: "Vollladung", 1: "WW/Heizung", 2: "Frostschutz", 3: "Warmwasser", 4: "Heizung"},
    0x1C11200F: {3: "Frostschutz"},
}

Entity = namedtuple("Entity", "ident key name kind unit device_class state_class divisor node")

log = logging.getLogger("can2mqtt")


def slug(text):
    return re.sub(r"[^a-z0-9]+", "_", text.lower()).strip("_")


def load_entities():
    """Signalliste -> alle Entitaeten, die grundsaetzlich gemeldet werden."""
    with open(SIGNALS, newline="") as f:
        rows = [row for row in csv.DictReader(f, delimiter=";")]
    chosen = []
    for row in rows:
        ident = int(row["id"], 16)
        node = ident >> 19 & 0x1F
        area = (node, row["modul"], row["instanz"])
        if (ident >> 24 != 0x1C or node not in DEVICES or area in EXCLUDE_AREAS
                or row["name"] in EXCLUDE_NAMES or "_PELL" in row["name"]
                or row["einheit"] in EXCLUDE_UNITS or ident in PAIRS.values()):
            continue
        chosen.append((ident, node, area, row))
    labels = Counter((node, row["bezeichnung"].strip()) for _, node, _, row in chosen)
    entities = []
    for ident, node, area, row in chosen:
        short = re.sub(r"^(BHZB30|EOS|SYSTEM)_", "", row["name"])
        key_prefix, name_prefix = AREAS.get(area, EOS_AREA)
        label = row["bezeichnung"].strip().rstrip(":")
        if ident in NAMES:
            key, name = NAMES[ident]
        else:
            key = key_prefix + slug(short)
            # ohne oder mit mehrfach vergebener Bezeichnung hilft nur der Signalname
            if not label or labels[(node, row["bezeichnung"].strip())] > 1:
                label = f"{label} ({short})".strip()
            name = name_prefix + label
        if ident not in VALIDATED:
            name += " *"
        unit, device_class, state_class = UNITS.get(row["einheit"], (None, None, None))
        if ident in TOTAL_INCREASING:
            state_class = "total_increasing"
        kind = ("binary_sensor" if row["einheit"] in BINARY_UNITS
                else "text" if row["einheit"] in TEXT_UNITS else "sensor")
        entities.append(Entity(ident, key, name, kind, unit, device_class, state_class,
                               DIVISORS.get(ident, int(row["teiler"])), node))
    return entities


def state_topic(node):
    return f"{BASE}/{DEVICES[node][0]}/state"


def discovery_messages(entities):
    for e in entities:
        config = {
            "name": e.name,
            "unique_id": f"brunner_bhz_can_{e.key}",
            "state_topic": state_topic(e.node),
            "availability_topic": AVAIL_TOPIC,
            "device": DEVICES[e.node][1],
            # Kommt laenger kein Wert, zeigt Home Assistant "nicht verfuegbar"
            "expire_after": int(MAX_INTERVAL * 3),
        }
        if e.device_class:
            config["device_class"] = e.device_class
        if e.kind == "binary_sensor":
            config["value_template"] = (
                f"{{{{ 'ON' if value_json.{e.key} == true else 'OFF' if value_json.{e.key} == false else 'None' }}}}"
            )
        else:
            config["value_template"] = f"{{{{ value_json.{e.key} }}}}"
            if e.unit:
                config["unit_of_measurement"] = e.unit
            if e.state_class:
                config["state_class"] = e.state_class
        component = "binary_sensor" if e.kind == "binary_sensor" else "sensor"
        yield f"homeassistant/{component}/brunner_bhz_can/{e.key}/config", json.dumps(config)


def switch_discovery_messages(entities):
    keys = {e.ident: e.key for e in entities}
    for key, switch in SWITCHES.items():
        if switch.state_id not in keys:
            continue
        state = f"value_json.{keys[switch.state_id]}"
        config = {
            "name": switch.name,
            "unique_id": f"brunner_bhz_can_{key}",
            "state_topic": state_topic(2),
            "command_topic": COMMAND_TOPIC + key,
            "availability_topic": AVAIL_TOPIC,
            "device": DEVICES[2][1],
            "value_template": (f"{{{{ 'ON' if {state} == {json.dumps(bool(switch.on))} "
                               f"else 'OFF' if {state} == {json.dumps(bool(switch.off))} else 'None' }}}}"),
        }
        yield f"homeassistant/switch/brunner_bhz_can/{key}/config", json.dumps(config)
    for key, select in SELECTS.items():
        if select.state_id not in keys:
            continue
        config = {
            "name": select.name,
            "unique_id": f"brunner_bhz_can_{key}",
            "state_topic": state_topic(2),
            "command_topic": COMMAND_TOPIC + key,
            "availability_topic": AVAIL_TOPIC,
            "device": DEVICES[2][1],
            "options": list(TEXTS[select.state_id].values()),
            "value_template": f"{{{{ value_json.{keys[select.state_id]} }}}}",
        }
        yield f"homeassistant/select/brunner_bhz_can/{key}/config", json.dumps(config)
    for key, number in NUMBERS.items():
        if number.state_id not in keys:
            continue
        config = {
            "name": number.name,
            "unique_id": f"brunner_bhz_can_{key}",
            "state_topic": state_topic(2),
            "command_topic": COMMAND_TOPIC + key,
            "availability_topic": AVAIL_TOPIC,
            "device": DEVICES[2][1],
            "min": number.low, "max": number.high, "step": number.step,
            "unit_of_measurement": number.unit,
            "value_template": f"{{{{ value_json.{keys[number.state_id]} }}}}",
        }
        yield f"homeassistant/number/brunner_bhz_can/{key}/config", json.dumps(config)


def number_value(number, text):
    """Eingabe aus Home Assistant -> Rohwert (Zehntel) oder None, wenn unzulaessig."""
    try:
        value = float(text)
    except ValueError:
        return None
    if not number.low <= value <= number.high or (value - number.low) % number.step:
        return None
    return round(value * 10)


def scale(raw, divisor):
    if divisor < 0:
        return raw * -divisor
    return raw / divisor if divisor > 1 else raw


class Bus:
    """Merkt sich je CAN-ID den letzten Rohwert (Byte 1-2 der Nutzdaten)."""

    def __init__(self):
        self.bus = socket.socket(socket.AF_CAN, socket.SOCK_RAW, socket.CAN_RAW)
        self.bus.bind((CAN_IF,))
        self.bus.settimeout(1.0)
        self.seen = {}  # ID -> (Rohbytes, Zeit)

    def read(self):
        """Liest bis zu 1 s lang einen Rahmen -> True, wenn einer kam."""
        try:
            frame = self.bus.recv(FRAME.size)
        except socket.timeout:
            return False
        ident, length, data = FRAME.unpack(frame)
        if length == 4:
            self.seen[ident & ID_MASK] = (data[1:3], time.monotonic())
        return True

    def send(self, ident, value):
        """Setzt einen Wert wie das Bedienteil: 3 Byte, Wert in Byte 1-2."""
        data = b"\x00" + value.to_bytes(2, "big", signed=True)
        self.bus.send(FRAME.pack(ident | EFF_FLAG, len(data), data))

    def listen(self, seconds):
        end = time.monotonic() + seconds
        while time.monotonic() < end:
            self.read()

    def raw(self, ident, signed=True):
        """-> frischer Rohwert oder None"""
        data, when = self.seen.get(ident, (None, 0))
        if data is None or time.monotonic() - when >= STALE_AFTER:
            return None
        return int.from_bytes(data, "big", signed=signed)

    def value(self, e):
        """-> Wert der Entitaet; None, wenn kein frischer Wert vorliegt."""
        if e.ident in PAIRS:
            low, high = self.raw(e.ident, signed=False), self.raw(PAIRS[e.ident], signed=False)
            if low is None or high is None:
                return None
            return scale(high << 16 | low, e.divisor)
        raw = self.raw(e.ident)
        if raw is None or (e.ident in BHZ_PROBES and raw >= MISSING_FROM):
            return None
        if e.kind == "binary_sensor":
            return bool(raw)
        if e.kind == "text":
            return TEXTS.get(e.ident, {}).get(raw, str(raw))
        value = scale(raw, e.divisor)
        if e.ident in ADD_TODAY:
            today = self.raw(TODAY)
            if today is None:
                return None
            value = round(value + today / 10, 1)
        return value

    def present(self, entities):
        """-> die Entitaeten, die auf dem Bus vorkommen und einen Wert haben."""
        return [e for e in entities if self.value(e) is not None]

    def values(self, entities):
        """-> Knoten -> Schluessel -> Wert"""
        values = {node: {} for node in DEVICES}
        for e in entities:
            values[e.node][e.key] = self.value(e)
        return values


def main():
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    bus = Bus()
    # gemeldet wird nur, was BHZ und EOS wirklich schicken; was erst spaeter
    # zum ersten Mal kommt, wird dann nachgemeldet
    candidates = load_entities()
    bus.listen(LISTEN_FIRST)
    entities = bus.present(candidates)
    if not entities:
        log.error("keine Daten vom CAN-Bus an %s", CAN_IF)
        return 1

    if "--once" in sys.argv:
        names = {e.key: e.name for e in entities}
        for node, values in bus.values(entities).items():
            print(f"== {DEVICES[node][1]['name']} ({len(values)})")
            for key, value in values.items():
                print(f"{names[key]:<60} {value}")
        return 0

    import paho.mqtt.client as mqtt

    client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id="can2mqtt")
    if MQTT_USER:
        client.username_pw_set(MQTT_USER, MQTT_PASSWORD)
    client.will_set(AVAIL_TOPIC, "offline", retain=True)

    def on_connect(client, userdata, flags, reason_code, properties):
        if reason_code.is_failure:
            log.error("MQTT-Anmeldung abgelehnt: %s", reason_code)
            return
        log.info("mit MQTT-Broker %s:%d verbunden, melde %d Werte", MQTT_HOST, MQTT_PORT, len(entities))
        for topic, payload in discovery_messages(entities):
            client.publish(topic, payload, retain=True)
        for topic, payload in switch_discovery_messages(entities):
            client.publish(topic, payload, retain=True)
        client.subscribe(COMMAND_TOPIC + "+")
        client.publish(AVAIL_TOPIC, "online", retain=True)

    def on_message(client, userdata, message):
        key = message.topic[len(COMMAND_TOPIC):]
        wanted = message.payload.decode(errors="replace")
        if key in SWITCHES and wanted in ("ON", "OFF"):
            target = SWITCHES[key]
            value = target.on if wanted == "ON" else target.off
        elif key in SELECTS and wanted in TEXTS[SELECTS[key].state_id].values():
            target = SELECTS[key]
            value = next(v for v, text in TEXTS[target.state_id].items() if text == wanted)
        elif key in NUMBERS and number_value(NUMBERS[key], wanted) is not None:
            target = NUMBERS[key]
            value = number_value(target, wanted)
        else:
            log.warning("unbekannter Schaltbefehl %s %r", message.topic, wanted)
            return
        current = bus.raw(target.state_id)
        # nur senden, wenn die BHZ einen anderen Zustand meldet - nie blind
        if current is None:
            log.warning("%s: Zustand unbekannt, sende nichts", key)
        elif current == value:
            log.info("%s steht schon auf %s", key, wanted)
        else:
            log.info("%s -> %s (sende %08X#00%04X)", key, wanted, target.set_id, value & 0xFFFF)
            bus.send(target.set_id, value)

    client.on_connect = on_connect
    client.on_message = on_message
    client.connect_async(MQTT_HOST, MQTT_PORT, keepalive=60)
    client.loop_start()

    sent = {node: None for node in DEVICES}
    sent_at = {node: 0.0 for node in DEVICES}
    last_frame, silent = time.monotonic(), False
    while True:
        now = time.monotonic()
        if bus.read():
            last_frame = now
            if silent:
                log.info("CAN-Bus liefert wieder Daten")
                silent = False
        elif not silent and now - last_frame > STALE_AFTER:
            log.warning("seit %d s keine Daten vom CAN-Bus", STALE_AFTER)
            silent = True
        if silent or now - min(sent_at.values()) < MIN_INTERVAL:
            continue
        known = {e.ident for e in entities}
        new = [e for e in bus.present(candidates) if e.ident not in known]
        if new:
            log.info("melde %d weitere Werte: %s", len(new), ", ".join(e.key for e in new))
            entities.extend(new)
            for topic, payload in discovery_messages(new):
                client.publish(topic, payload, retain=True)
            # Schalter haengen an ihrem gemeldeten Wert; der kann jetzt erst da sein
            for topic, payload in switch_discovery_messages(entities):
                client.publish(topic, payload, retain=True)
        for node, values in bus.values(entities).items():
            if now - sent_at[node] < MIN_INTERVAL:
                continue
            if values != sent[node] or now - sent_at[node] >= MAX_INTERVAL:
                client.publish(state_topic(node), json.dumps(values))
                sent[node], sent_at[node] = values, now


if __name__ == "__main__":
    sys.exit(main())
