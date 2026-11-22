# Womo-Zentrale – 7"-Display für den Dethleffs XXL A

Raspberry Pi mit 7-Zoll-Touchdisplay. Zeigt alles vom Cerbo GX, das Wetter am aktuellen Standort und steuert die SHR-Hubstützen.

## Seiten

| Seite | Inhalt |
|---|---|
| **Start** | Deine Illustration des XXL A als Schnittbild (`static/img/womo.webp`) mit Live-Werten an den Bauteilen: Innenraum mit Außentemperatur, Kühlschrank mit Gefrierfach (orange bei über 8 °C bzw. über −12 °C), Solar, alle drei Akkus einzeln, MultiPlus, Grau- und Frischwasser, Landstrom. Leuchtpunkte zeigen den Energiefluss, die Solarmodule glühen bei Sonne. Rechts Wetter, Schnelltasten mit Kurzinfo (Niveau, Klima, Radio, Wecker, Energie) und die Uhr. Bei Böen ab 40 km/h: „Markise einfahren!“. Ohne Bild erscheint das 3D-Modell. Eigenes Bild tauschen: Datei `static/img/womo.webp` ersetzen (Format 2:1). |
| **Energie** | Energiefluss Solar / Landstrom / Booster → MultiPlus / Batterie → Verbraucher. Jeder Solarregler einzeln: Leistung, PV-Spannung, Zustand, Ertrag heute und gestern |
| **Batterie** | die **drei LiTime-Akkus per Bluetooth** (SOC, Spannung, Strom, Temperatur, Zyklen, Zellspannungen) und jede Batterie bzw. jeder Shunt: SOC, Spannung, Strom, Temperatur, Restzeit, Ladezyklen, Zellspannungen, Starterbatterie, Alarme |
| **Landstrom** | MultiPlus: Modus (AN / Nur Laden / Nur WR / AUS), Landstrom-Begrenzung mit Schnellwahl 4–25 A, Eingangs- und Ausgangswerte, Generator Start/Stopp (falls vorhanden) |
| **Tanks** | alle Tanks mit Litern |
| **Klima** | Gree-Dachklimaanlage: Ein/Aus, Solltemperatur, Kühlen/Heizen/Entfeuchten/Lüften/Auto, Lüfterstufe, Leise, Turbo, Licht; alle Temperatur- und Feuchtesensoren |
| **Schalter** | Relais des Cerbo, schaltbare Ausgänge, Ladebooster (Orion XS), Ladegeräte, digitale Eingänge (Tür, Klappen) |
| **Niveau** | Wasserwaage quer und längs mit Keil-Angabe in cm, Kalibrieren; SHR AutoLevel (Auto-Nivellieren, Einfahren); **Bewegungsalarm mit Push aufs Handy** |
| **Wetter** | aktuell, 24-Stunden-Verlauf mit Regenwahrscheinlichkeit, 7 Tage, Böen, Sonnenauf- und -untergang, UV. Der Ort kommt automatisch (GPS → WLAN-Ortung → Internet) oder per **„📍 Ort ändern“** |
| **Uhr** | große Uhr, Timer mit Schnellwahl, Wecker mit Wochentagen und Schlummern |
| **Radio** | Webradio: Top-Sender Deutschland, Pop, Rock, Schlager, Nachrichten, Oldies, 80er, Jazz, Klassik; Favoriten |
| **Licht** | Grundriss des Womos mit großen Lichtschaltern (Shelly), Dimmen, Szenen. ⚙ Einrichten: Lampen benennen, Platz wählen, Shelly zuordnen |
| **Extras** | **Abfahrt** (Checkliste, Warnung beim Losfahren), **Reisekarte** (Strecke und Stellplätze), **Verlauf** (Akku, Solar, Verbrauch, Akku-Prognose, Tagesbilanz), **Wartung** (Termine mit Erinnerung), **Warnungen** (Grenzwerte einstellen), **Handy** (QR-Code, Fernzugang) |
| **System** | **alle Werte**, die der Cerbo liefert, nach Gerät sortiert und durchsuchbar; Helligkeit, Nachtmodus, GPS |

Alles, was etwas schaltet, startet nur nach **gedrückt halten**. Oben in der Kopfzeile erscheinen Alarme und Warnungen aller Geräte.

---

## 1. Einkaufsliste

| Teil | Hinweis |
|---|---|
| **Raspberry Pi 5, 4 GB** | 2 GB reichen auch |
| **Raspberry Pi Touch Display 2, 7"** (720 × 1280) | z. B. BerryBase, ca. 64,50 € |
| **Aktiver Kühler für den Pi 5** | offizieller „Active Cooler“ |
| **microSD-Karte 32 GB** (Klasse A2) | |
| **Spannungswandler 12 V → 5 V USB-C, mind. 5 A** | für die Bordbatterie |
| **Gehäuse / Einbaurahmen für das Touch Display 2** | für die Wandmontage |
| **Lagesensor MPU-6050** (Platine „GY-521“) + 4 Steckkabel | für Wasserwaage und Bewegungsalarm, ca. 5 € – **oder** einfacher: WitMotion **WT61C-USB-TTL** (USB, ca. 33 €) |
| **Lautsprecher** (USB oder Bluetooth) | für Radio, Wecker und Timer – der Pi 5 hat keinen Kopfhörerausgang |
| *optional:* **USB-GPS-Maus am Cerbo** | damit Wetter und Ort unterwegs automatisch stimmen |

Das kleine runde ESP32-Display wird nicht mehr gebraucht.

---

## 2. Cerbo GX vorbereiten

1. **MQTT einschalten:** *Einstellungen → Integrationen → MQTT* (ältere Firmware: *Dienste → MQTT im LAN*).
2. **Sicherheitsprofil** (*Einstellungen → Allgemein → Zugang & Sicherheit*): „Schwach“ (dann das GX-Passwort in `config.json` eintragen) oder „Ungesichert“.
3. **Relais** zum Schalten auf Funktion **„Manuell“** stellen.
4. **Cerbo ins Womo-WLAN** bringen und im Router eine **feste IP-Adresse** für den Cerbo vergeben. Diese IP kommt in `config.json`.

---

## 3. Raspberry Pi einrichten

1. Mit dem **Raspberry Pi Imager** „Raspberry Pi OS (64-bit)“ **mit Desktop** auf die SD-Karte schreiben. Im Imager gleich das **WLAN** und einen Benutzer eintragen.
2. Display anschließen (Flachbandkabel an DSI, Stromkabel an die GPIO-Pins), Pi starten.
3. Den Ordner `womo-zentrale` auf den Pi kopieren (z. B. per USB-Stick nach `/home/<benutzer>/womo-zentrale`).
4. Terminal öffnen und ausführen:
   ```
   cd ~/womo-zentrale
   bash install.sh
   ```
5. `config.json` öffnen (Doppelklick) und anpassen:
   - `cerbo.host`: IP-Adresse des Cerbo
   - `cerbo.password`: GX-Passwort, nur bei Sicherheitsprofil „Schwach“
   - `weather.fallback_*`: Ort, der ohne GPS für das Wetter genommen wird
6. **Querformat:** *Menü → Einstellungen → Bildschirmkonfiguration* → Display „DSI-1“ → *Ausrichtung* auf **Rechts** (oder Links), dann anwenden. Der Touch dreht automatisch mit.
7. `sudo reboot` – danach startet die Oberfläche automatisch im Vollbild.

Von jedem Handy oder Laptop im WLAN ist die Oberfläche zusätzlich unter `http://<IP-des-Pi>:8080` erreichbar.

---

## 4. Lagesensor anschließen (Wasserwaage und Bewegungsalarm)

**Am einfachsten: USB-Sensor WitMotion WT61C-USB-TTL** (Variante „TTL“ mit USB-Kabel, *nicht* „232“ oder „485“).
Sensor flach und fest im Fahrzeug anschrauben, USB-Kabel in den Pi stecken – fertig. Die Zentrale erkennt ihn
innerhalb von 15 Sekunden von selbst. Danach auf ebenem Platz unter **Niveau → Kalibrieren** drücken.

**Alternativ: MPU-6050 an der Stiftleiste:**

| MPU-6050 | Raspberry Pi (Stiftleiste) |
|---|---|
| VCC | Pin 1 (3,3 V) |
| GND | Pin 6 (GND) |
| SDA | Pin 3 (GPIO 2) |
| SCL | Pin 5 (GPIO 3) |

Den Sensor **flach und fest** im Fahrzeug montieren (z. B. hinter dem Display an der Wand oder im Möbel). Auf ebenem Grund auf der Seite „Niveau“ **Kalibrieren** gedrückt halten. Zeigt links/rechts oder vorne/hinten verkehrt herum an: in `config.json` unter `level` die Werte `invert_roll`, `invert_pitch` bzw. `swap_axes` auf `true` setzen.

## 5. Bewegungsalarm aufs Handy

1. App **ntfy** installieren (Google Play / App Store, kostenlos, ohne Konto).
2. In der App „Thema abonnieren“ und diesen Kanal eintragen: **`womo-xxla-a3f5763d48`** (steht auch in `config.json` unter `alarm.ntfy_topic`).
3. Auf der Seite „Niveau“ **Test-Push** tippen – die Nachricht muss aufs Handy kommen.
4. **Scharf schalten** (gedrückt halten). Nach 10 Sekunden Ruhezeit meldet jede Neigungsänderung ab 0,4° eine Push-Nachricht „Bewegung erkannt!“ (höchste Priorität, alle 5 Minuten wiederholt, solange es sich bewegt). Empfindlichkeit: `alarm.threshold_deg`.

Der Kanalname ist zufällig gewählt – wer ihn kennt, kann mitlesen. Nicht weitergeben.

## 6. LiTime-Akkus (Bluetooth)

1. LiTime-App auf dem Handy schließen (ein Akku erlaubt nur eine Bluetooth-Verbindung).
2. Auf dem Display **System → Akkus suchen** tippen (oder auf der Seite „Batterie“). Der Pi sucht etwa 12 Sekunden.
3. Gefundene Akkus stehen oben und sind grün markiert. Bei jedem auf **Akku 1**, **Akku 2** oder **Akku 3** tippen – fertig, das wird gespeichert.

Danach liest der Pi die Akkus bei jedem Start automatisch, nacheinander alle 30 Sekunden. Falsch zugeordnet? „Entfernen“ gedrückt halten und neu zuordnen.

## Temperaturfühler (Innenraum, Außen, Kühlschrank, Gefrierfach)

Die Temperaturen kommen vom Cerbo GX, z. B. über Ruuvi-Funksensoren (Bluetooth) oder Temperaturfühler an den Cerbo-Eingängen. Damit die Anzeige sie richtig zuordnet, im Cerbo den Namen vergeben: *Einstellungen → Geräte → Sensor → Name* mit „**Innenraum**“, „**Außen**“, „**Kühlschrank**“ und „**Gefrierfach**“ (oder „Tiefkühl“). Auf der Startseite stehen Innen- und Außentemperatur im Kasten „Innenraum“ (ohne Außenfühler nimmt die Anzeige die Wetter-Temperatur), Kühlschrank und Gefrierfach im Kasten „Kühlschrank“; auf der Seite „Klima“ stehen alle Fühler.

## 7. Gree-Klimaanlage

1. Die Klima zuerst mit der App **Gree+** ins Womo-WLAN bringen.
2. Die Womo-Zentrale sucht das Gerät automatisch. Klappt das nicht, im Router die IP-Adresse der Klima ablesen und in `config.json` unter `gree.ip` eintragen.

Die Steuerung läuft lokal im WLAN, ohne Cloud.

---

## 8. Hubstützen (SHR AutoLevel)

Die Seite „Stützen“ ist fertig. Es fehlen nur die zwei Bluetooth-Befehle der SHR-Steuerung.

1. **Mitschnitt mit einem Android-Handy:** In den Entwickleroptionen „Bluetooth-HCI-Snoop-Protokoll“ einschalten und Bluetooth aus- und einschalten. In der SHR-App einmal **Auto-Nivellieren** und einmal **Einfahren** drücken. Danach einen *Fehlerbericht* erstellen und die ZIP-Datei an Claude schicken.
2. **Auf dem Pi:** SHR-App am Handy trennen und dann ausführen:
   ```
   python3 tools/shr_scan.py              # zeigt alle Bluetooth-Geräte
   python3 tools/shr_scan.py AA:BB:CC:..  # Details der SHR-Steuerung
   ```
   Die Ausgabe ebenfalls an Claude schicken.
3. Claude trägt die Werte in `config.json` unter `shr` ein (`enabled: true`). Danach ausführen: `sudo systemctl restart womo-zentrale`.

---

## Bildschirmtastatur

Die Womo-Zentrale hat eine eigene Tastatur (QWERTZ mit Umlauten). Sie fährt automatisch von unten herein, sobald du in ein Eingabefeld tippst. Oben siehst du, was du tippst; „Fertig“ blendet sie wieder aus. ⇧ einmal = ein Großbuchstabe, zweimal = Feststelltaste. „?123“ schaltet auf Zahlen und Sonderzeichen.

Erscheint zusätzlich die Raspberry-Tastatur (Squeekboard), diese ausschalten: *Raspberry Pi Konfiguration → Display → On-screen Keyboard → Disabled*.

## Vollbild ein/aus

Auf der Startseite rechts unten **„⛶ Vollbild ein / aus“**, auf allen anderen Seiten das Symbol **⛶** oben rechts neben der Uhr. Damit wechselst du zwischen Vollbild und normalem Fenster. **Das Vollbild verlassen geht nur mit PIN** (ab Werk **1234**, ändern in `config.json` bei `"fullscreen_pin"`, danach `sudo systemctl restart womo-zentrale`) – z. B. um an den Desktop, das Terminal oder die Einstellungen des Pi zu kommen. Nochmal tippen = wieder Vollbild.

**Aktualisieren einer bestehenden Installation:** neue ZIP woanders entpacken (z. B. „Downloads“), dann `server.py`, `install.sh` und `static/index.html` in den vorhandenen Ordner `~/womo-zentrale` kopieren (**nicht** `config.json`!) und im Terminal ausführen:
```
cd ~/womo-zentrale
bash install.sh
sudo reboot
```

## Automatische Updates (GitHub)

Die Womo-Zentrale holt sich neue Versionen selbst aus dem GitHub-Projekt **womo-zentrale**. Sie schaut alle 5 Minuten nach (`update_interval_min` in config.json); gibt es etwas Neues, wird es installiert, das Programm startet neu und die Anzeige lädt sich von selbst neu. **Deine Einstellungen bleiben dabei immer erhalten** (`config.json`, Wecker, Kalibrierung – diese Dateien liegen nur auf dem Pi und nie bei GitHub).

Auf der Seite **System → Updates** siehst du die Version und kannst mit „Jetzt nach Updates suchen“ sofort aktualisieren. Steht dort „Bitte einmal bash install.sh ausführen“, braucht das Update neue Pakete: Terminal öffnen, `cd ~/womo-zentrale` und `bash install.sh`.

Automatik ausschalten: in `config.json` `"auto_update": false` setzen (dann nur noch per Taste).

## Extras

**Warnungen aufs Handy** (über die gleiche ntfy-App wie der Bewegungsalarm): Akku niedrig / fast leer, Frischwasser fast leer,
Grau- und Toilettentank fast voll, Landstrom weg, Kühlschrank oder Gefrierfach zu warm, Frost in den nächsten 12 Stunden,
Sturmböen in den nächsten 6 Stunden. Jede Warnung kommt einmal und erst wieder, wenn sie vorher weg war.
Grenzwerte und An/Aus unter **Extras → Warnungen**. Aktive Warnungen stehen oben in der Kopfzeile und auf der Startseite.

**Akku-Prognose**: rechnet mit dem Durchschnittsverbrauch der letzten 24 Stunden und der Solar-Vorhersage für die nächsten Tage.
Akku-Kapazität und Solarleistung unter **Extras → Verlauf** einstellen; nach ein paar sonnigen Tagen lernt die Zentrale selbst,
was die Module wirklich bringen. „Leer“ heißt: 10 % Restladung.

**Abfahrts-Checkliste**: Punkte antippen zum Abhaken, eigene Punkte ergänzen. Mit GPS am Cerbo erkennt die Zentrale das
Losfahren und meldet offene Punkte sofort (Bildschirm und Handy) – und ganz besonders ein noch eingestecktes Landstromkabel.
2 Stunden nach der Ankunft werden die Haken zurückgesetzt.

**Reisekarte**: mit GPS werden Strecke, Kilometer und Stellplätze (ab 1 Stunde Standzeit) automatisch aufgezeichnet.
Ohne GPS kannst du Stellplätze mit „📍 Aktuellen Ort als Stellplatz eintragen“ selbst festhalten.

**Wartung**: „Erledigt“ gedrückt halten → nächster Termin wird berechnet. Mit ‹ › verschieben. Erinnerung 30 Tage,
7 Tage vorher und am Tag selbst.

**Handy**: QR-Code scannen (Handy im gleichen WLAN) – die Zentrale läuft dann auch im Handy-Browser. Für unterwegs
(von zu Hause aus) Tailscale einrichten, die Anleitung steht direkt auf der Seite.

**Standort**: Reihenfolge GPS (Cerbo) → selbst gewählter Ort → WLAN-Ortung → Internet-Standort → Geldern.
Die WLAN-Ortung schickt die Kennungen der umliegenden WLAN-Netze (keine Inhalte) an die offene Datenbank beacondb.net.
Abschalten: in `config.json` bei `weather` den Wert `"auto_location": false` setzen.

## Licht (Shelly)

Geeignet für 12 V: **Shelly Plus RGBW PM** (4 dimmbare Lichtkreise je Gerät) und **Shelly Plus 1** (an/aus).
1. Shellys mit der Shelly-App ins **gleiche WLAN wie der Pi** bringen.
2. In der Zentrale **Licht → ⚙ Einrichten → „Shellys im WLAN suchen“**.
3. Lampe links auswählen, Namen und Platz im Womo festlegen, dann den passenden Shelly-Kanal antippen.
Beim RGBW PM in der Shelly-App den Modus **„Licht“ (4 Kanäle)** wählen, damit jede Lampe einzeln dimmbar ist.

## Sprachsteuerung

Hardware: USB-Mikrofon, am besten **reSpeaker XVF3800 USB** (Lautsprecher an dessen Klinkenbuchse).
Einrichten: **Extras → Sprache → „Sprachsteuerung einrichten“** – lädt Spracherkennung und Stimme (ca. 150 MB) ohne Terminal.
Danach: „**Zentrale**, Licht aus“, „Zentrale, wie voll ist der Akku?“ usw. – alles läuft offline auf dem Pi.
Ohne Weckwort: Mikrofon-Taste unten rechts antippen.

## Sensoren und Kamera

Die Zentrale hört per Bluetooth automatisch mit: **Mopeka Pro Check** (Gas), Reifendruck-Ventilkappen **„TPMS1_…“**,
**Shelly BLU Door/Window** und andere BTHome-Sensoren (Fenster, Türen, Wassermelder, Bewegung).
Unter **Extras → Sensoren** tauchen sie unter „Gefunden“ auf – antippen und Rolle wählen (z. B. „Gasflasche 1“, „Reifen vorne links“).
Warnungen: Gas fast leer, Reifendruck zu niedrig, Wasser erkannt; Fenster/Tür geöffnet bei scharfem Bewegungsalarm.
**Extras → Kamera**: Reolink-Kamera mit IP-Adresse, Benutzer und Passwort eintragen.

## 9. Hilfe

| Problem | Lösung |
|---|---|
| Punkt oben rechts rot, überall „--“ | Cerbo-IP, MQTT und Sicherheitsprofil prüfen; Status steht auf der Seite „System“ |
| „MQTT abgelehnt“ | Profil „Schwach“ → GX-Passwort in `config.json` |
| Wetter fehlt | Internet im WLAN prüfen; ohne GPS wird der Ersatzort aus `config.json` genommen |
| Helligkeit ändert sich nicht | nach `install.sh` einmal neu starten |
| Startseite zeigt keine 3D-Ansicht | Internet beim Installieren nötig (three.js); sonst erscheint die flache Ansicht |
| Kein Ton | Lautsprecher anschließen und in der Taskleiste als Ausgabe wählen |
| Programm-Meldungen ansehen | `journalctl -u womo-zentrale -f` |
| Neu starten nach Änderungen | `sudo systemctl restart womo-zentrale` |
