#!/usr/bin/env python3
"""Liest die Home-Seite der Brunner BHZ per VNC und meldet die Werte per MQTT
(Home Assistant MQTT Discovery).

Einstellungen ueber Umgebungsvariablen:
    BHZ_HOST, BHZ_PORT          VNC-Adresse der BHZ
    MQTT_HOST, MQTT_PORT        Broker
    MQTT_USER, MQTT_PASSWORD    optional
    INTERVAL                    Sekunden zwischen zwei Abfragen (Standard 30)
    AUTO_HOME_AFTER             steht die Anzeige so viele Sekunden nicht auf
                                "Heizung -> Home", wird dorthin geklickt
                                (Standard 300, 0 = nie klicken)
    --once                      einmal lesen, Werte ausgeben, nichts senden
"""
import json
import logging
import os
import sys
import time
from pathlib import Path

import numpy as np
from PIL import Image

import rfb
import screen

BHZ_HOST = os.environ.get("BHZ_HOST", "192.168.178.20")
BHZ_PORT = int(os.environ.get("BHZ_PORT", "5900"))
MQTT_HOST = os.environ.get("MQTT_HOST", "localhost")
MQTT_PORT = int(os.environ.get("MQTT_PORT", "1883"))
MQTT_USER = os.environ.get("MQTT_USER")
MQTT_PASSWORD = os.environ.get("MQTT_PASSWORD")
INTERVAL = float(os.environ.get("INTERVAL", "30"))
AUTO_HOME_AFTER = float(os.environ.get("AUTO_HOME_AFTER", "300"))

BASE = "brunner/bhz"
STATE_TOPIC = f"{BASE}/state"
AVAIL_TOPIC = f"{BASE}/available"
UNKNOWN_DIR = Path(__file__).parent / "unknown"

DEVICE = {"identifiers": ["brunner_bhz"], "name": "Brunner BHZ", "manufacturer": "Brunner", "model": "BHZ 2"}

# Schluessel -> (Anzeigename, Art, Einheit, device_class)
ENTITIES = {
    "puffer_oben": ("Puffer oben", "sensor", "°C", "temperature"),
    "puffer_mitte": ("Puffer Mitte", "sensor", "°C", "temperature"),
    "puffer_unten": ("Puffer unten", "sensor", "°C", "temperature"),
    "aussentemperatur": ("Außentemperatur", "sensor", "°C", "temperature"),
    "heizkreis_1": ("Heizkreis 1", "sensor", "°C", "temperature"),
    "gastherme": ("Gastherme", "sensor", "°C", "temperature"),
    "kachelofen_temperatur": ("Kachelofen Temperatur", "sensor", "°C", "temperature"),
    "kachelofen_leistung": ("Kachelofen Leistung", "sensor", "kW", "power"),
}

log = logging.getLogger("bhz2mqtt")


def discovery_messages():
    for key, (name, kind, unit, device_class) in ENTITIES.items():
        config = {
            "name": name,
            "unique_id": f"brunner_bhz_{key}",
            "state_topic": STATE_TOPIC,
            "availability_topic": AVAIL_TOPIC,
            "device": DEVICE,
            "device_class": device_class,
            # Kommt laenger kein Wert, zeigt Home Assistant "nicht verfuegbar"
            "expire_after": int(max(INTERVAL * 6, 180)),
        }
        if kind == "sensor":
            config["value_template"] = f"{{{{ value_json.{key} }}}}"
            config["unit_of_measurement"] = unit
            config["state_class"] = "measurement"
        else:
            config["value_template"] = (
                f"{{{{ 'ON' if value_json.{key} == true else 'OFF' if value_json.{key} == false else 'None' }}}}"
            )
        yield f"homeassistant/{kind}/brunner_bhz/{key}/config", json.dumps(config)


def save_unknown(readings):
    """Hebt nicht erkannte Zeichen fuer learn.py auf (hoechstens 200 Dateien)."""
    for name, reading in readings.items():
        for glyph in reading.unknown:
            UNKNOWN_DIR.mkdir(exist_ok=True)
            if len(list(UNKNOWN_DIR.iterdir())) >= 200:
                return
            data = (glyph * 255).round().astype(np.uint8)
            path = UNKNOWN_DIR / f"{name}_{abs(hash(data.tobytes())) % 10**10}.png"
            if not path.exists():
                Image.fromarray(data).save(path)


class Poller:
    def __init__(self):
        self.not_home_since = None
        self.last_problem = None

    def _note(self, problem):
        """Meldet einen Zustand nur beim Wechsel, damit das Log lesbar bleibt."""
        if problem != self.last_problem:
            (log.warning if problem else log.info)(problem or "Home-Seite wird wieder gelesen")
            self.last_problem = problem

    def poll(self):
        """-> Werte der Home-Seite oder None, wenn sie gerade nicht lesbar ist."""
        with rfb.RFB(BHZ_HOST, BHZ_PORT) as vnc:
            img = vnc.capture()
            if not screen.is_home(img):
                now = time.monotonic()
                self.not_home_since = self.not_home_since or now
                waited = now - self.not_home_since
                click = screen.next_click_towards_home(img)
                if AUTO_HOME_AFTER and waited >= AUTO_HOME_AFTER and click:
                    log.info("Anzeige seit %d s nicht auf Home, klicke %s", waited, click)
                    vnc.click(*click)
                    return None
                self._note("Anzeige steht nicht auf Heizung -> Home"
                           + ("" if click else " (unbekannter Bildschirm, kein Klick)"))
                return None
        self.not_home_since = None
        values, raw, readings = screen.read_home(img)
        save_unknown(readings)
        unreadable = {name: reading.text for name, reading in readings.items() if reading.text is None}
        guessed = {name: raw[name] for name, reading in readings.items() if reading.guessed}
        if unreadable:
            self._note(f"nicht lesbar: {sorted(unreadable)} (Zeichen in {UNKNOWN_DIR})")
        elif guessed:
            self._note(f"mit ungelerntem Zeichen gelesen: {guessed}")
        else:
            self._note(None)
        return values


def main():
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    poller = Poller()

    if "--once" in sys.argv:
        values = poller.poll()
        print(json.dumps(values, indent=2, ensure_ascii=False))
        return 0 if values else 1

    import paho.mqtt.client as mqtt

    client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id="bhz2mqtt")
    if MQTT_USER:
        client.username_pw_set(MQTT_USER, MQTT_PASSWORD)
    client.will_set(AVAIL_TOPIC, "offline", retain=True)

    def on_connect(client, userdata, flags, reason_code, properties):
        if reason_code.is_failure:
            log.error("MQTT-Anmeldung abgelehnt: %s", reason_code)
            return
        log.info("mit MQTT-Broker %s:%d verbunden", MQTT_HOST, MQTT_PORT)
        for topic, payload in discovery_messages():
            client.publish(topic, payload, retain=True)
        client.publish(AVAIL_TOPIC, "online", retain=True)

    client.on_connect = on_connect
    client.connect_async(MQTT_HOST, MQTT_PORT, keepalive=60)
    client.loop_start()

    vnc_down = False
    while True:
        started = time.monotonic()
        try:
            values = poller.poll()
            if vnc_down:
                log.info("BHZ wieder erreichbar")
                vnc_down = False
            if values is not None:
                client.publish(STATE_TOPIC, json.dumps(values))
        except (OSError, rfb.RFBError) as exc:
            if not vnc_down:
                log.warning("BHZ nicht erreichbar: %s", exc)
                vnc_down = True
        time.sleep(max(1.0, INTERVAL - (time.monotonic() - started)))


if __name__ == "__main__":
    sys.exit(main())
