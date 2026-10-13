# Brunner BHZ 2 in Home Assistant

Bindet eine Brunner Heizzentrale **BHZ 2** (mit Kachelofen-Abbrandsteuerung
EOS) in Home Assistant ein. Die BHZ 2 hat keine Modbus- oder
Smart-Home-Schnittstelle; dieses Projekt nutzt stattdessen ihren CAN-Bus und –
als älteren, zweiten Weg – ihre Bildschirmanzeige per VNC.

Das ist ein privates Projekt ohne Verbindung zur Firma Brunner. Es ist an
genau einer Anlage entstanden (BHZ 2, Firmware 2.7.118, Kachelofen mit EOS,
Gastherme als Zusatzheizung, ein Heizkreis, kein Solar). An einer anderen
Anlage wird einiges anzupassen sein. Benutzung auf eigene Gefahr.

## Was drin ist

| Ordner | Zweck |
|---|---|
| `can/` | **Empfohlener Weg.** Hört den CAN-Bus mit und meldet alle Werte per MQTT; einige Einstellungen lassen sich schalten. |
| `reader/` | Liest die Home-Seite der Anzeige per VNC und Texterkennung, meldet acht Werte per MQTT. Braucht keine Hardware, ist aber langsamer und ungenauer. |
| `web/` | Stellt die Anzeige der BHZ per noVNC als Webseite bereit, z. B. für ein Home-Assistant-Dashboard. |
| `novnc/` | noVNC als Git-Submodul (für `web/`). |

Alle Teile melden sich über **MQTT Discovery** selbst in Home Assistant an;
dort muss nur die MQTT-Integration eingerichtet sein.

## CAN-Bus (`can/`)

### Was man braucht

- einen Rechner mit Linux in der Nähe der Heizung, z. B. einen Raspberry Pi
- einen CAN-Adapter, den Linux als SocketCAN-Schnittstelle kennt (hier:
  CANable mit candleLight-Firmware, meldet sich als `gs_usb`)
- Anschluss an CAN-H, CAN-L und Masse des Brunner-Busses
- einen MQTT-Broker (z. B. mosquitto) und Home Assistant mit MQTT-Integration

Der Bus läuft mit **125 kbit/s**. Hängt der Adapter nur mitten am Bus mit
dran, bleibt sein Abschlusswiderstand aus.

### Erst einmal nur mithören

```bash
sudo apt install can-utils python3-venv
sudo ip link set can0 up type can bitrate 125000 listen-only on
candump -L can0 | head
```

Mit `listen-only` sendet der Adapter nichts, auch keine Bestätigungen. Kommen
Zeilen mit IDs, die mit `1C10…` oder `1C18…` anfangen, stimmt der Anschluss.
Lesbar werden sie so:

```bash
timeout 10 candump -L can0 | can/candecode.py        # nur Werte ungleich 0
timeout 10 candump -L can0 | can/candecode.py -a     # alle
```

### Dienst einrichten

```bash
sudo mkdir -p /opt/brunner && sudo chown $USER: /opt/brunner
cp -r can /opt/brunner/
cd /opt/brunner
python3 -m venv .venv
.venv/bin/pip install -r can/requirements.txt
.venv/bin/python can/can2mqtt.py --once        # 10 s mithören, Werte anzeigen
```

In `can/brunner-can.service` Benutzer und `MQTT_HOST` anpassen, dann:

```bash
sudo cp can/can0.service can/brunner-can.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now can0 brunner-can
```

`can0.service` fährt die Schnittstelle hoch, sobald der Adapter da ist (auch
nach einem Neustart oder Umstecken). Wer nur lesen will, trägt dort
`listen-only on` ein; die Schalter funktionieren dann nicht.

Einstellungen von `can2mqtt.py` (Umgebungsvariablen): `CAN_IF`, `MQTT_HOST`,
`MQTT_PORT`, `MQTT_USER`, `MQTT_PASSWORD`, `MIN_INTERVAL`, `MAX_INTERVAL`.

### Was in Home Assistant erscheint

Zwei Geräte, **Brunner BHZ CAN** und **Brunner EOS CAN**, mit zusammen rund
180 Werten: Puffer-, Außen-, Vorlauf- und Kesseltemperaturen, Brennraum,
Pumpen und Mischer, Erträge des Kachelofens, Status und Einstellungen von
Heizkreis und Gastherme und vieles mehr.

Gemeldet wird alles aus der Signalliste `can/bhz2_signale.csv`, was auf dem Bus
tatsächlich vorkommt. Ausgenommen sind PIN-Werte, nicht angeschlossene Fühler
und die Anlagenteile, die es an der Ursprungsanlage nicht gibt (Solar,
Heizkreis 2, Pellet, Zusatzheizung 2 – siehe `EXCLUDE_AREAS` und
`EXCLUDE_NAMES` in `can2mqtt.py`).

**Ein Stern (`*`) am Namen** heißt: Zuordnung oder Bedeutung dieses Werts ist
noch nicht mit der Anzeige der BHZ verglichen. Die Namen stammen dann nur aus
der Signalliste und können an der eigenen Anlage etwas anderes bedeuten.
Geprüfte Werte stehen in `VALIDATED`. Auswahl- und Statuswerte erscheinen als
Zahl, solange ihr Text nicht in `TEXTS` eingetragen ist.

Schaltbar sind bisher:

| Element | Art |
|---|---|
| Heizkreis 1 Ein/Aus | Schalter |
| Gastherme Ein/Aus | Schalter |
| Gastherme aktiviert für (Vollladung, WW/Heizung, Frostschutz, Warmwasser, Heizung) | Auswahl |
| Heizkreis 1 Wärmer/Kälter (−10 … +10 °C) | Zahl |

Der Dienst sendet dafür genau die Nachricht, die das Bedienteil beim
Umstellen schickt, und nur, wenn die BHZ gerade einen anderen Zustand meldet.
Sonst sendet er nichts auf den Bus.

### Topics

| Topic | Inhalt |
|---|---|
| `brunner/can/bhz/state`, `brunner/can/eos/state` | alle Werte als JSON |
| `brunner/can/available` | `online` / `offline` |
| `brunner/can/set/<name>` | Befehle (`ON`/`OFF`, Auswahltext oder Zahl) |
| `homeassistant/…/brunner_bhz_can/…/config` | Discovery |

### An eine andere Anlage anpassen

- **Andere Ausstattung:** `EXCLUDE_AREAS` und `EXCLUDE_NAMES` in
  `can2mqtt.py` anpassen.
- **Werte prüfen:** mit der Anzeige vergleichen, dann die ID in `VALIDATED`
  eintragen; Texte für Auswahlwerte in `TEXTS`.
- **Weitere Einstellungen schaltbar machen:** `candump -L can0` laufen lassen,
  den Wert am Bedienteil umstellen und die Nachricht heraussuchen, die dabei
  geschickt wurde (siehe Protokoll unten). Dann in `SWITCHES`, `SELECTS` oder
  `NUMBERS` eintragen.
- **BHZ 3 oder andere Parametersätze:** `can/ndef2csv.py` erzeugt die
  Signalliste aus einer `para_export.xml` der Brunner-Firmware. Für die BHZ 3
  gibt es außerdem die Signalliste in
  [tillsc/brunner_can](https://github.com/tillsc/brunner_can), der dieses
  Projekt den Einstieg verdankt.

### Protokoll in Kürze

Erweiterte CAN-IDs (29 Bit), Protokoll „gecan" von Garz & Fricke:

| Bits | Bedeutung |
|---|---|
| 24–28 | Art der Nachricht (`0x1C` = Wert vom Gerät) |
| 19–23 | Knoten (2 = BHZ, 3 = EOS) |
| 13–18 | Modul |
| 10–12 | Instanz |
| 8–9 | 0 = Wert, 2 = Setzen oder Abfragen |
| 0–7 | Parameternummer |

- **Werte:** Die Geräte senden alles von selbst, die meisten Werte alle 1–4 s,
  manche seltener. Nutzdaten 4 Byte; der Wert steht als vorzeichenbehaftete
  16-Bit-Zahl in Byte 1–2 (Big Endian). Der Teiler steht in der Signalliste;
  ein negativer Teiler heißt malnehmen. Temperaturen kommen in Zehntelgrad,
  aber nur mit 1 °C Auflösung. Rohwert 4000 bei einem Fühler = nicht
  angeschlossen.
- **Setzen:** Das Bedienteil schickt `0B…` (oder `13…`) mit Typ 2 und 3 Byte,
  Wert in Byte 1–2, z. B. `0B112224#000001`. Die BHZ antwortet mit `0A…`
  (bzw. `12…`) und meldet danach den neuen Wert unter der `1C…`-ID.
- **Erträge:** „letzte 5 Tage / 4 Wochen / 6 Monate" zeigt die Anzeige
  einschließlich des heutigen Tags; `can2mqtt.py` rechnet das ebenso.

## Anzeige als Webseite (`web/`)

Die BHZ stellt ihre Anzeige per VNC bereit (Port 5900, 640×480).
`web/run-web.sh` macht daraus mit noVNC eine Webseite, die sich auch bedienen
lässt.

noVNC liegt als Git-Submodul in `novnc/`; nach einem normalen `git clone` ist
der Ordner leer und wird mit dem ersten Befehl gefüllt.

```bash
git submodule update --init
python3 -m venv .venv && .venv/bin/pip install websockify
BHZ_HOST=<Adresse der BHZ> LISTEN=0.0.0.0:6080 web/run-web.sh
```

Dann `http://<rechner>:6080/` öffnen: Die Startseite (`web/index.html`) bietet
„Nur ansehen" und „Bedienen". Als Dienst: `web/brunner-web.service`.
Die Seite ist ungeschützt – nur im eigenen Netz freigeben.

## Anzeige auslesen (`reader/`)

Der ursprüngliche Weg ohne CAN-Adapter: `reader/bhz2mqtt.py` holt alle 30 s
ein Bild der Seite „Heizung → Home" und liest daraus die drei
Puffertemperaturen, Außentemperatur, Heizkreis 1, Gastherme und Kachelofen
(Temperatur oder Leistung).

```bash
python3 -m venv .venv && .venv/bin/pip install -r reader/requirements.txt
cd reader
BHZ_HOST=<Adresse der BHZ> ../.venv/bin/python bhz2mqtt.py --once
```

Als Dienst: `reader/brunner-reader.service`. Einstellungen stehen oben in
`bhz2mqtt.py`.

Einschränkungen:

- Gelesen wird nur, solange die Anzeige auf der Home-Seite steht. Nach
  `AUTO_HOME_AFTER` Sekunden klickt der Dienst von selbst dorthin zurück.
- Die Texterkennung vergleicht mit Zeichen, die einmal vom echten Bildschirm
  gelernt wurden (`reader/glyphs/`). Unbekannte Zeichen landen in
  `reader/unknown/` und lassen sich mit `learn.py` nachlernen. Das
  Minuszeichen ist noch nicht vom echten Bildschirm gelernt.
- Die Positionen der Felder gelten für die Home-Seite der Ursprungsanlage.

`reader/snapshot.py` fotografiert alle Seiten der Anzeige mit Zeitstempel –
praktisch, um einen gleichzeitig laufenden CAN-Mitschnitt mit dem zu
vergleichen, was die Anzeige zeigt.

## Hinweise

- **Installateur-Einstellungen:** Auf dem Bus sind auch Parameter der
  Installateur-Ebene zu sehen. Dieses Projekt liest sie nur. Wer eigene
  Schalter ergänzt, sollte bei dem bleiben, was sich am Bedienteil ohne
  Installateur-PIN einstellen lässt.
- **Senden auf den Bus:** Eine falsch gebaute Nachricht kann eine andere
  Einstellung der Heizung verändern. Neue Befehle immer erst am Bedienteil
  mitschneiden und nicht raten.

## Lizenz

MIT, siehe `LICENSE`. noVNC (Submodul) steht unter seiner eigenen Lizenz
(MPL 2.0, siehe `novnc/LICENSE.txt`).
