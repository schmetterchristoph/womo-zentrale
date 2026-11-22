#!/usr/bin/env python3
"""Womo-Zentrale – Backend für das 7"-Display im Wohnmobil.

* holt ALLE Werte vom Cerbo GX per MQTT (N/<portal-id>/#)
* schickt Schaltbefehle an den Cerbo (W/<portal-id>/...)
* lädt das Wetter für den aktuellen Standort (Open-Meteo, ohne Schlüssel)
* löst die SHR-AutoLevel-Funktionen per Bluetooth aus
* liest die LiTime-LiFePO4-Akkus per Bluetooth aus
* misst die Neigung des Fahrzeugs (Lagesensor MPU-6050 am Pi) für die Wasserwaage
* speichert die Wecker
* Bewegungsalarm über den Lagesensor mit Push-Nachricht aufs Handy (ntfy)
* steuert die Gree-Dachklimaanlage im WLAN (Bibliothek greeclimate, lokal ohne Cloud)
* liefert die Oberfläche (static/index.html) und spricht per WebSocket mit ihr
"""
import asyncio
import json
import logging
import math
import os
import re
import glob
import shutil
import subprocess
import threading
import time
from pathlib import Path

import aiohttp
from aiohttp import web
import paho.mqtt.client as mqtt

try:
    from bleak import BleakClient, BleakScanner
except ImportError:  # Bluetooth ist optional
    BleakClient = BleakScanner = None

BASE = Path(__file__).resolve().parent
# Eigene Einstellungen liegen in config.json (wird nie überschrieben).
# Fehlt sie, wird sie aus config.example.json angelegt.
if not (BASE / "config.json").exists():
    (BASE / "config.json").write_text((BASE / "config.example.json").read_text(encoding="utf-8"), encoding="utf-8")
CFG = json.loads((BASE / "config.json").read_text(encoding="utf-8"))
# Neue Einstellungen aus späteren Updates ergänzen, eigene Werte bleiben
try:
    _ex = json.loads((BASE / "config.example.json").read_text(encoding="utf-8"))
    for _k, _v in _ex.items():
        if _k not in CFG:
            CFG[_k] = _v
        elif isinstance(_v, dict) and isinstance(CFG[_k], dict):
            for _k2, _v2 in _v.items():
                CFG[_k].setdefault(_k2, _v2)
except Exception:
    pass
_new_topic = False
if not CFG.get("alarm", {}).get("ntfy_topic"):
    import secrets
    CFG.setdefault("alarm", {})["ntfy_topic"] = "womo-" + secrets.token_hex(6)
    _new_topic = True
if _new_topic:
    (BASE / "config.json").write_text(json.dumps(CFG, indent=2, ensure_ascii=False), encoding="utf-8")
log = logging.getLogger("womo")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

# ---------------------------------------------------------------------------
# Zustand
# ---------------------------------------------------------------------------
values: dict[str, object] = {}        # "service/inst/pfad" -> Wert
changed: dict[str, object] = {}
lock = threading.Lock()
state = {
    "cerbo": "Verbinde ...",
    "portal_id": CFG["cerbo"].get("portal_id", ""),
    "last_msg": 0.0,
    "weather": None,
    "weather_place": CFG["weather"]["fallback_name"],
    "shr": "Befehle noch nicht eingerichtet" if not CFG["shr"]["enabled"] else "Bereit",
    "shr_busy": False,
    "lithium": {},          # Name -> Daten des Akkus
    "level": None,          # {"roll":.., "pitch":.., "ok": bool}
    "ac": {"ok": False, "status": "Suche Klimaanlage ..."},
    "guard": {"armed": False, "triggered": False, "base": None, "since": 0, "last_push": 0},
}
ble_lock = asyncio.Lock()   # nur eine Bluetooth-Verbindung gleichzeitig
ALARM_FILE = BASE / "alarms.json"
LEVEL_FILE = BASE / "level_calibration.json"
clients: set[web.WebSocketResponse] = set()

# Nur diese Pfade dürfen von der Oberfläche geschrieben werden
WRITABLE = [re.compile(p) for p in (
    r"^vebus/\d+/Mode$",
    r"^vebus/\d+/Ac/ActiveIn/CurrentLimit$",
    r"^vebus/\d+/Ac/In/\d+/CurrentLimit$",
    r"^system/0/Relay/\d+/State$",
    r"^dcdc/\d+/Mode$",
    r"^solarcharger/\d+/Mode$",
    r"^charger/\d+/Mode$",
    r"^generator/0/ManualStart$",
    r"^switch/\d+/SwitchableOutput/[^/]+/State$",
    r"^settings/0/Settings/CGwacs/BatteryLife/MinimumSocLimit$",
)]

# ---------------------------------------------------------------------------
# Cerbo GX (MQTT)
# ---------------------------------------------------------------------------
try:  # paho-mqtt 2.x
    mq = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id=f"womo-zentrale-{os.getpid()}")
except AttributeError:  # paho-mqtt 1.x (Raspberry Pi OS Bookworm)
    mq = mqtt.Client(client_id=f"womo-zentrale-{os.getpid()}")
if CFG["cerbo"].get("password"):
    mq.username_pw_set(CFG["cerbo"].get("user") or "womo", CFG["cerbo"]["password"])
first_keepalive = True


def on_connect(client, userdata, flags, rc, props=None):
    global first_keepalive
    if rc != 0:
        state["cerbo"] = f"MQTT abgelehnt ({rc}) – Passwort / Sicherheitsprofil prüfen"
        return
    state["cerbo"] = "Verbunden"
    first_keepalive = True
    if state["portal_id"]:
        client.subscribe(f"N/{state['portal_id']}/#")
        send_keepalive()
    else:
        state["cerbo"] = "Suche Portal-ID ..."
        client.subscribe("N/+/system/0/Serial")


def on_disconnect(*args):
    state["cerbo"] = "Verbindung getrennt"


def on_message(client, userdata, msg):
    parts = msg.topic.split("/", 2)
    if len(parts) < 3:
        return
    if not state["portal_id"]:
        if msg.topic.endswith("/system/0/Serial"):
            state["portal_id"] = parts[1]
            client.unsubscribe("N/+/system/0/Serial")
            client.subscribe(f"N/{parts[1]}/#")
            state["cerbo"] = "Verbunden"
            send_keepalive()
        return
    try:
        val = json.loads(msg.payload)["value"] if msg.payload else None
    except Exception:
        return
    key = parts[2]
    with lock:
        values[key] = val
        changed[key] = val
    state["last_msg"] = time.time()


def send_keepalive():
    global first_keepalive
    pid = state["portal_id"]
    if not pid or not mq.is_connected():
        return
    payload = "" if first_keepalive else json.dumps({"keepalive-options": ["suppress-republish"]})
    mq.publish(f"R/{pid}/keepalive", payload)
    first_keepalive = False


def cerbo_write(path: str, value) -> bool:
    if not any(p.match(path) for p in WRITABLE):
        log.warning("Schreiben verweigert: %s", path)
        return False
    if not state["portal_id"]:
        return False
    mq.publish(f"W/{state['portal_id']}/{path}", json.dumps({"value": value}))
    log.info("Cerbo schreiben %s = %s", path, value)
    return True


async def keepalive_loop():
    while True:
        send_keepalive()
        await asyncio.sleep(30)


# ---------------------------------------------------------------------------
# Wetter (Open-Meteo)
# ---------------------------------------------------------------------------
IP_LOC = {"lat": None, "lon": None, "place": None, "at": 0}


def gps_position():
    """Standort: 1. GPS vom Cerbo, 2. Internet-Standort (WLAN/Mobilfunk), 3. fester Ort aus config."""
    for k, v in list(values.items()):
        if k.startswith("gps/") and k.endswith("/Position/Latitude") and v is not None:
            lon = values.get(k.replace("Latitude", "Longitude"))
            if lon is not None:
                return float(v), float(lon), "gps"
    if IP_LOC["lat"] is not None:
        return IP_LOC["lat"], IP_LOC["lon"], "internet"
    return CFG["weather"]["fallback_lat"], CFG["weather"]["fallback_lon"], "fest"


async def update_ip_location(s):
    """Ungefähren Standort über die Internet-Verbindung bestimmen (alle 15 Minuten)."""
    if not CFG["weather"].get("auto_location", True) or time.time() - IP_LOC["at"] < 900:
        return
    IP_LOC["at"] = time.time()
    sources = (
        ("http://ip-api.com/json/?lang=de&fields=status,lat,lon,city", lambda d: (d.get("lat"), d.get("lon"), d.get("city"))),
        ("https://ipapi.co/json/", lambda d: (d.get("latitude"), d.get("longitude"), d.get("city"))),
        ("https://ipwho.is/?lang=de", lambda d: (d.get("latitude"), d.get("longitude"), d.get("city"))),
    )
    for url, pick in sources:
        try:
            async with s.get(url, timeout=aiohttp.ClientTimeout(total=10)) as r:
                lat, lon, city = pick(await r.json(content_type=None))
            if lat is not None and lon is not None:
                IP_LOC.update(lat=float(lat), lon=float(lon), place=city)
                log.info("Standort über Internet: %s (%.3f, %.3f)", city, lat, lon)
                return
        except Exception as e:
            log.info("Standort über %s nicht ermittelt: %s", url.split("/")[2], e)


def dist_km(a, b):
    return math.hypot((a[0] - b[0]) * 111, (a[1] - b[1]) * 111 * math.cos(math.radians(a[0])))


async def weather_loop():
    last_pos, last_fetch, last_src = None, 0, None
    async with aiohttp.ClientSession(headers={"User-Agent": "Womo-Zentrale/1.0"}) as s:
        while True:
            await update_ip_location(s)
            lat, lon, src = gps_position()
            moved = last_pos is None or src != last_src or dist_km(last_pos, (lat, lon)) > 5
            if moved or time.time() - last_fetch > 900:
                try:
                    url = ("https://api.open-meteo.com/v1/forecast"
                           f"?latitude={lat:.4f}&longitude={lon:.4f}"
                           "&current=temperature_2m,relative_humidity_2m,apparent_temperature,weather_code,"
                           "wind_speed_10m,wind_gusts_10m,wind_direction_10m,precipitation,is_day,pressure_msl"
                           "&hourly=temperature_2m,precipitation_probability,weather_code,wind_gusts_10m"
                           "&daily=weather_code,temperature_2m_max,temperature_2m_min,precipitation_sum,"
                           "precipitation_probability_max,wind_gusts_10m_max,sunrise,sunset,uv_index_max"
                           "&timezone=auto&forecast_days=7&wind_speed_unit=kmh")
                    async with s.get(url, timeout=aiohttp.ClientTimeout(total=20)) as r:
                        state["weather"] = await r.json()
                        state["weather"]["fetched"] = time.time()
                        state["weather"]["loc_source"] = src
                    if moved:
                        place = None
                        if src == "fest":
                            place = CFG["weather"].get("fallback_name") or "Standort"
                        else:
                            try:
                                url = (f"https://nominatim.openstreetmap.org/reverse?format=json&zoom=10"
                                       f"&lat={lat:.4f}&lon={lon:.4f}&accept-language=de")
                                async with s.get(url, timeout=aiohttp.ClientTimeout(total=15)) as r:
                                    a = (await r.json()).get("address", {})
                                    place = (a.get("city") or a.get("town") or a.get("village")
                                             or a.get("municipality") or a.get("county"))
                            except Exception as e:
                                log.info("Ortsname nicht ermittelt: %s", e)
                            place = place or (IP_LOC["place"] if src == "internet" else None) or "Standort"
                        state["weather_place"] = place
                        log.info("Wetter für %s (Quelle: %s)", place, src)
                    last_pos, last_fetch, last_src = (lat, lon), time.time(), src
                    await broadcast({"type": "weather", "weather": state["weather"], "place": state["weather_place"]})
                except Exception as e:
                    log.warning("Wetter nicht geladen: %s", e)
            await asyncio.sleep(60)


# ---------------------------------------------------------------------------
# SHR AutoLevel (Bluetooth)
# ---------------------------------------------------------------------------
async def shr_send(cmd: str):
    c = CFG["shr"]
    if not c["enabled"] or BleakClient is None:
        state["shr"] = "Befehle noch nicht eingerichtet"
        return
    if state["shr_busy"]:
        return
    state["shr_busy"] = True
    got = False
    try:
        data = bytes.fromhex(c["cmd_level_hex"] if cmd == "level" else c["cmd_retract_hex"])
        await ble_lock.acquire()
        got = True
        state["shr"] = "Suche Steuerung ..."
        await push_status()
        if c.get("device_address"):
            dev = await BleakScanner.find_device_by_address(c["device_address"], timeout=10)
        else:
            dev = await BleakScanner.find_device_by_filter(
                lambda d, ad: (d.name or "").startswith(c["device_name"]), timeout=10)
        if not dev:
            state["shr"] = "Steuerung nicht gefunden"
            return
        state["shr"] = "Verbinde ..."
        await push_status()
        async with BleakClient(dev) as cl:
            await cl.write_gatt_char(c["char_uuid"], data, response=True)
        state["shr"] = "Nivellierung gestartet" if cmd == "level" else "Einfahren gestartet"
        log.info("SHR-Befehl gesendet: %s", cmd)
    except Exception as e:
        state["shr"] = f"Fehler: {e}"
        log.warning("SHR: %s", e)
    finally:
        if got:
            ble_lock.release()
        state["shr_busy"] = False
        await push_status()


# ---------------------------------------------------------------------------
# LiTime-Akkus (Bluetooth, Bibliothek litime-ble)
# ---------------------------------------------------------------------------
def _flatten(d, pre=""):
    out = {}
    for k, v in (d or {}).items():
        key = f"{pre}{k}"
        if isinstance(v, dict):
            out.update(_flatten(v, key + "."))
        elif isinstance(v, list) and v and all(isinstance(x, (int, float)) for x in v):
            for i, x in enumerate(v):
                out[f"{key}.{i + 1}"] = x
        elif isinstance(v, (int, float, str, bool)) or v is None:
            out[key] = v
    return out


async def lithium_loop():
    """Liest die zugeordneten Akkus nacheinander. Neue Zuordnungen gelten sofort."""
    try:
        from litime_ble import BatteryClient
    except ImportError:
        log.warning("litime-ble nicht installiert – Akkus werden nicht gelesen")
        return
    while True:
        c = CFG.get("lithium", {})
        bats = [b for b in c.get("batteries", []) if b.get("address")] if c.get("enabled", True) else []
        if not bats:
            await asyncio.sleep(5)
            continue
        for b in bats:
            name = b.get("name") or b["address"]
            try:
                async with ble_lock:
                    client = BatteryClient(address=b["address"])
                    async with client.session():
                        st = await asyncio.wait_for(client.read_once_async(), 20)
                data = st.json() if hasattr(st, "json") else st
                if isinstance(data, str):
                    data = json.loads(data)
                state["lithium"][name] = {"ok": True, "t": time.time(), "data": _flatten(data)}
            except Exception as e:
                log.warning("Akku %s: %s", name, e)
                old = state["lithium"].get(name, {})
                state["lithium"][name] = {**old, "ok": False, "err": str(e)[:80]}
            await broadcast({"type": "lithium", "lithium": state["lithium"]})
            await asyncio.sleep(2)
        await asyncio.sleep(max(10, int(c.get("interval", 30))))


# ---------------------------------------------------------------------------
# Akkus suchen und zuordnen (Seite "System" -> "Akkus suchen")
# ---------------------------------------------------------------------------
LIKELY_BAT = re.compile(r"litime|^l-?\d|^lt|lifepo|^12v|^24v|bms|ampere|redodo|power ?queen", re.I)
scan_state = {"busy": False}


def save_config():
    tmp = BASE / "config.json.tmp"
    tmp.write_text(json.dumps(CFG, indent=2, ensure_ascii=False), encoding="utf-8")
    tmp.replace(BASE / "config.json")


def bat_slots():
    bats = CFG.setdefault("lithium", {}).setdefault("batteries", [])
    while len(bats) < 3:
        bats.append({"name": f"Akku {len(bats) + 1}", "address": ""})
    return bats


async def bat_scan():
    if scan_state["busy"] or BleakScanner is None:
        return
    scan_state["busy"] = True
    await broadcast({"type": "batscan", "busy": True, "found": []})
    found = []
    try:
        async with ble_lock:
            res = await BleakScanner.discover(timeout=12, return_adv=True)
        mine = {b.get("address", "").upper() for b in bat_slots()}
        for addr, (d, ad) in res.items():
            name = d.name or ad.local_name or ""
            found.append({"address": addr, "name": name or "(ohne Namen)", "rssi": ad.rssi,
                          "likely": bool(LIKELY_BAT.search(name)), "assigned": addr.upper() in mine})
        found.sort(key=lambda x: (not x["likely"], -x["rssi"]))
    except Exception as e:
        log.warning("Bluetooth-Suche: %s", e)
    finally:
        scan_state["busy"] = False
    await broadcast({"type": "batscan", "busy": False, "found": found[:40]})


async def bat_assign(slot: int, address: str, name: str | None):
    bats = bat_slots()
    if not 0 <= slot < len(bats):
        return
    for b in bats:                         # dieselbe Adresse nicht doppelt
        if address and b.get("address", "").upper() == address.upper():
            b["address"] = ""
    old = bats[slot].get("name")
    bats[slot]["address"] = address
    if name:
        bats[slot]["name"] = name
    CFG["lithium"]["enabled"] = True
    state["lithium"].pop(old, None)
    save_config()
    log.info("Akku %d zugeordnet: %s", slot + 1, address or "(leer)")
    await push_status()
    await broadcast({"type": "lithium", "lithium": state["lithium"]})


# ---------------------------------------------------------------------------
# Wasserwaage (MPU-6050 an I2C, Adresse 0x68)
# ---------------------------------------------------------------------------
def load_level_cal():
    try:
        return json.loads(LEVEL_FILE.read_text())
    except Exception:
        return {"roll": 0.0, "pitch": 0.0}


class I2CLevel:
    """MPU-6050 an den Pi-Stiften (I2C)."""
    name = "MPU-6050 (I2C)"

    def __init__(self, lc):
        try:
            from smbus2 import SMBus
        except ImportError:
            from smbus import SMBus
        self.bus = SMBus(int(lc.get("i2c_bus", 1)))
        self.addr = int(str(lc.get("address", "0x68")), 16)
        self.bus.write_byte_data(self.addr, 0x6B, 0)      # aufwecken
        self.bus.write_byte_data(self.addr, 0x1A, 6)      # Tiefpass 5 Hz

    async def read(self):
        d = self.bus.read_i2c_block_data(self.addr, 0x3B, 6)
        return [((d[i] << 8 | d[i + 1]) ^ 0x8000) - 0x8000 for i in (0, 2, 4)]

    def close(self):
        try:
            self.bus.close()
        except Exception:
            pass


class WitLevel:
    """WitMotion-Neigungssensor (z. B. WT61C/WT901C, TTL mit USB-Kabel) an USB."""
    BAUDS = (115200, 9600, 230400, 921600)

    def __init__(self, dev):
        import termios
        self.dev, self.buf, self.acc = dev, b"", None
        self.fd = os.open(dev, os.O_RDONLY | os.O_NOCTTY | os.O_NONBLOCK)
        self.termios = termios
        self.name = f"WitMotion (USB {os.path.basename(dev)})"

    def set_baud(self, baud):
        t = self.termios
        a = t.tcgetattr(self.fd)
        a[0] = 0; a[1] = 0; a[3] = 0                       # roh: keine Umwandlungen
        a[2] = t.CS8 | t.CREAD | t.CLOCAL
        sp = getattr(t, f"B{baud}")
        a[4] = a[5] = sp
        t.tcsetattr(self.fd, t.TCSANOW, a)
        t.tcflush(self.fd, t.TCIFLUSH)
        self.buf = b""

    def _pump(self):
        try:
            while True:
                chunk = os.read(self.fd, 512)
                if not chunk:
                    break
                self.buf += chunk
        except BlockingIOError:
            pass
        found = False
        b = self.buf
        while len(b) >= 11:
            if b[0] != 0x55:
                b = b[1:]
                continue
            pkt = b[:11]
            if sum(pkt[:10]) & 0xFF != pkt[10]:
                b = b[1:]
                continue
            if pkt[1] == 0x51:                             # Beschleunigung
                self.acc = [int.from_bytes(pkt[i:i + 2], "little", signed=True) for i in (2, 4, 6)]
                found = True
            b = b[11:]
        self.buf = b[-64:] if len(b) > 64 else b
        return found

    async def detect(self):
        for baud in self.BAUDS:
            self.set_baud(baud)
            for _ in range(15):
                await asyncio.sleep(.1)
                if self._pump():
                    log.info("WitMotion-Sensor an %s mit %d Baud", self.dev, baud)
                    return True
        return False

    async def read(self):
        for _ in range(20):
            if self._pump():
                break
            await asyncio.sleep(.02)
        if self.acc is None:
            raise IOError("keine Daten vom USB-Sensor")
        return self.acc

    def close(self):
        try:
            os.close(self.fd)
        except Exception:
            pass


LEVEL_CAL = {"roll": 0.0, "pitch": 0.0}


async def find_level_sensor(lc):
    """Erst I2C (MPU-6050) probieren, dann USB-Sensoren (WitMotion)."""
    want = str(lc.get("sensor", "auto")).lower()
    if want in ("auto", "i2c", "mpu6050"):
        try:
            return I2CLevel(lc)
        except Exception as e:
            if want != "auto":
                log.info("Kein I2C-Lagesensor: %s", e)
    if want in ("auto", "usb", "witmotion"):
        devs = [lc["usb_device"]] if lc.get("usb_device") else \
            sorted(glob.glob("/dev/ttyUSB*")) + sorted(glob.glob("/dev/ttyACM*"))
        for dev in devs:
            try:
                w = WitLevel(dev)
            except Exception as e:
                log.info("USB %s nicht lesbar: %s", dev, e)
                continue
            if await w.detect():
                return w
            w.close()
    return None


async def level_loop():
    lc = CFG.get("level", {})
    if not lc.get("enabled", True):
        return
    cal = LEVEL_CAL
    cal.update(load_level_cal())
    told = False
    while True:
        sensor = await find_level_sensor(lc)
        if not sensor:
            if not told:
                log.info("Kein Lagesensor gefunden – Wasserwaage aus (suche alle 15 s weiter, USB oder I2C)")
                told = True
            state["level"] = {"ok": False}
            await asyncio.sleep(15)
            continue
        log.info("Lagesensor gefunden: %s", sensor.name)
        told = False
        roll_f = pitch_f = None
        n = errors = 0
        while errors < 10:
            try:
                ax, ay, az = await sensor.read()
                errors = 0
                if lc.get("swap_axes"):
                    ax, ay = ay, ax
                roll = math.degrees(math.atan2(ay, az))
                pitch = math.degrees(math.atan2(-ax, math.hypot(ay, az)))
                if lc.get("invert_roll"):
                    roll = -roll
                if lc.get("invert_pitch"):
                    pitch = -pitch
                roll_f = roll if roll_f is None else roll_f + (roll - roll_f) * .15
                pitch_f = pitch if pitch_f is None else pitch_f + (pitch - pitch_f) * .15
                state["level"] = {"ok": True, "sensor": sensor.name, "roll_raw": roll_f, "pitch_raw": pitch_f,
                                  "roll": roll_f - cal["roll"], "pitch": pitch_f - cal["pitch"]}
                await guard_check(roll_f, pitch_f)
                n += 1
                if n % 4 == 0:
                    await broadcast({"type": "level", "level": state["level"]})
            except Exception as e:
                errors += 1
                log.warning("Lagesensor: %s", e)
                await asyncio.sleep(1)
            await asyncio.sleep(.05)
        log.warning("Lagesensor %s antwortet nicht mehr – suche neu", sensor.name)
        sensor.close()
        state["level"] = {"ok": False}
        await broadcast({"type": "level", "level": state["level"]})


# ---------------------------------------------------------------------------
# Bewegungsalarm
# ---------------------------------------------------------------------------
def guard_public():
    g = state["guard"]
    return {"armed": g["armed"], "triggered": g["triggered"], "since": g["since"],
            "push": bool(CFG.get("alarm", {}).get("ntfy_topic"))}


async def push(title: str, text: str, prio: str = "urgent", detail: bool = False):
    c = CFG.get("alarm", {})
    topic = c.get("ntfy_topic")
    res = (False, "Kein Push-Kanal eingetragen")
    if topic:
        server = c.get("ntfy_server", "https://ntfy.sh").rstrip("/")
        try:
            async with aiohttp.ClientSession() as s:
                async with s.post(server, timeout=aiohttp.ClientTimeout(total=15),
                                  json={"topic": topic, "title": title, "message": text,
                                        "priority": {"min": 1, "low": 2, "default": 3, "high": 4, "urgent": 5}.get(prio, 3),
                                        "tags": ["rotating_light"]}) as r:
                    body = (await r.text())[:120]
                    res = (r.status < 300, "Gesendet" if r.status < 300 else f"Fehler {r.status}: {body}")
        except Exception as e:
            res = (False, f"Keine Verbindung zu ntfy ({type(e).__name__})")
        log.info("Push '%s': %s", title, res[1])
    return res if detail else res[0]


async def guard_set(armed: bool):
    g = state["guard"]
    g.update({"armed": armed, "triggered": False, "base": None, "since": time.time() if armed else 0})
    await broadcast({"type": "guard", "guard": guard_public()})
    if armed:
        await push("Womo: Alarm scharf", "Der Bewegungsalarm ist eingeschaltet.", "low")


async def guard_check(roll, pitch):
    g, c = state["guard"], CFG.get("alarm", {})
    if not g["armed"]:
        return
    if g["base"] is None or time.time() - g["since"] < 10:     # 10 s Ruhezeit nach dem Scharfschalten
        g["base"] = (roll, pitch)
        return
    d = max(abs(roll - g["base"][0]), abs(pitch - g["base"][1]))
    if d >= float(c.get("threshold_deg", 0.4)):
        now = time.time()
        first = not g["triggered"]
        g["triggered"] = True
        if first:
            await broadcast({"type": "guard", "guard": guard_public()})
        if now - g["last_push"] > float(c.get("repeat_min", 5)) * 60:
            g["last_push"] = now
            ok = await push("Womo: Bewegung erkannt!",
                            f"Der Lagesensor hat eine Bewegung von {d:.1f}° gemessen ({time.strftime('%H:%M:%S')}).")
            log.warning("Bewegungsalarm ausgelöst (%.2f°), Push %s", d, "gesendet" if ok else "fehlgeschlagen")


def level_calibrate():
    lv = state.get("level") or {}
    if lv.get("ok"):
        c = {"roll": lv["roll_raw"], "pitch": lv["pitch_raw"]}
        LEVEL_FILE.write_text(json.dumps(c))
        LEVEL_CAL.update(c)
        log.info("Wasserwaage kalibriert")


# ---------------------------------------------------------------------------
# Gree-Klimaanlage (WLAN, lokal)
# ---------------------------------------------------------------------------
gree = {"dev": None, "lock": None}
GREE_MODES = ["auto", "cool", "dry", "fan", "heat"]
GREE_FANS = ["auto", "low", "mediumlow", "medium", "mediumhigh", "high"]


def gree_state():
    d = gree["dev"]
    if not d:
        return state["ac"]
    def g(attr, default=None):
        try:
            return getattr(d, attr)
        except Exception:
            return default
    mode, fan = g("mode"), g("fan_speed")
    return {"ok": True, "status": "Verbunden", "name": d.device_info.name or "Gree",
            "power": bool(g("power")), "mode": GREE_MODES[int(mode)] if mode is not None else None,
            "target": g("target_temperature"), "current": g("current_temperature"),
            "fan": GREE_FANS[int(fan)] if fan is not None else None,
            "turbo": bool(g("turbo")), "quiet": bool(g("quiet")), "light": bool(g("light")),
            "swing_v": int(g("vertical_swing", 0) or 0)}


async def gree_loop():
    c = CFG.get("gree", {})
    if not c.get("enabled"):
        state["ac"] = {"ok": False, "status": "nicht eingerichtet"}
        return
    try:
        from greeclimate.discovery import Discovery
        from greeclimate.device import Device, DeviceInfo
    except ImportError:
        state["ac"] = {"ok": False, "status": "greeclimate nicht installiert"}
        return
    gree["lock"] = asyncio.Lock()
    while True:
        try:
            if not gree["dev"]:
                info = None
                if c.get("ip"):
                    info = DeviceInfo(c["ip"], int(c.get("port", 7000)), c.get("mac", ""), c.get("name", "Gree"))
                else:
                    found = await Discovery().scan(wait_for=8)
                    info = found[0] if found else None
                if not info:
                    state["ac"] = {"ok": False, "status": "Klimaanlage nicht gefunden"}
                    await broadcast({"type": "ac", "ac": state["ac"]})
                    await asyncio.sleep(60)
                    continue
                dev = Device(info)
                await dev.bind()
                gree["dev"] = dev
                log.info("Gree verbunden: %s", info)
            async with gree["lock"]:
                await gree["dev"].update_state()
            state["ac"] = gree_state()
        except Exception as e:
            log.warning("Gree: %s", e)
            state["ac"] = {"ok": False, "status": f"Fehler: {str(e)[:60]}"}
            gree["dev"] = None
        await broadcast({"type": "ac", "ac": state["ac"]})
        await asyncio.sleep(10)


async def gree_set(cmd: dict):
    d = gree["dev"]
    if not d:
        return
    from greeclimate.device import Mode, FanSpeed
    async with gree["lock"]:
        if "power" in cmd:
            d.power = bool(cmd["power"])
        if cmd.get("mode") in GREE_MODES:
            d.mode = Mode(GREE_MODES.index(cmd["mode"]))
        if "target" in cmd:
            d.target_temperature = int(max(16, min(30, round(float(cmd["target"])))))
        if cmd.get("fan") in GREE_FANS:
            d.fan_speed = FanSpeed(GREE_FANS.index(cmd["fan"]))
        for k, attr in (("turbo", "turbo"), ("quiet", "quiet"), ("light", "light")):
            if k in cmd:
                setattr(d, attr, bool(cmd[k]))
        await d.push_state_update()
    state["ac"] = gree_state()
    await broadcast({"type": "ac", "ac": state["ac"]})


# ---------------------------------------------------------------------------
# Wecker
# ---------------------------------------------------------------------------
def load_alarms():
    try:
        return json.loads(ALARM_FILE.read_text())
    except Exception:
        return []


# ---------------------------------------------------------------------------
# Bildschirmhelligkeit (Touch Display 2)
# ---------------------------------------------------------------------------
def backlight_dir():
    base = Path("/sys/class/backlight")
    return next(iter(sorted(base.iterdir())), None) if base.exists() else None


def set_brightness(percent: float):
    d = backlight_dir()
    if not d:
        return
    mx = int((d / "max_brightness").read_text())
    try:
        (d / "brightness").write_text(str(max(1, round(mx * percent / 100))))
    except PermissionError:
        log.warning("Keine Rechte für die Helligkeit (install.sh ausführen)")


# ---------------------------------------------------------------------------
# Vollbild umschalten (schickt F11 an den Browser auf dem Pi-Bildschirm)
# ---------------------------------------------------------------------------
async def toggle_fullscreen():
    rd = f"/run/user/{os.getuid()}"
    env = dict(os.environ, XDG_RUNTIME_DIR=rd)
    socks = sorted(glob.glob(rd + "/wayland-[0-9]"))
    if socks and shutil.which("wtype"):
        env["WAYLAND_DISPLAY"] = os.path.basename(socks[0])
        cmd = ["wtype", "-k", "F11"]
    elif shutil.which("xdotool"):
        env.setdefault("DISPLAY", ":0")
        cmd = ["xdotool", "key", "F11"]
    else:
        log.warning("Vollbild: wtype/xdotool fehlt (install.sh erneut ausführen)")
        return
    try:
        p = await asyncio.create_subprocess_exec(*cmd, env=env)
        await asyncio.wait_for(p.wait(), 5)
    except Exception as e:
        log.warning("Vollbild: %s", e)


# ---------------------------------------------------------------------------
# Automatische Updates von GitHub
# Der Ordner ist ein Git-Klon. Alle 5 Minuten (update_interval_min) wird nachgeschaut; gibt es eine
# neue Version, werden die Programmdateien ersetzt (config.json bleibt) und das
# Programm beendet sich – systemd startet es sofort mit der neuen Version neu.
# ---------------------------------------------------------------------------
NEEDS_INSTALL = BASE / ".needs_install"
PAUSED = BASE / ".update_paused"          # nach einem Zurückstufen: keine Auto-Updates
state["update"] = {"version": "", "available": False, "status": ""}


async def git(*args):
    p = await asyncio.create_subprocess_exec("git", *args, cwd=BASE, stdout=asyncio.subprocess.PIPE,
                                             stderr=asyncio.subprocess.PIPE)
    out, err = await asyncio.wait_for(p.communicate(), 120)
    return p.returncode, (out or err).decode(errors="ignore").strip()


async def check_update(apply: bool):
    u = state["update"]
    if not (BASE / ".git").exists() or not shutil.which("git"):
        u.update(status="Updates nicht eingerichtet (kein GitHub-Ordner)")
        return
    _, local = await git("rev-parse", "HEAD")
    u["version"] = local[:7]
    rc, msg = await git("fetch", "--quiet", "origin")
    if rc != 0:
        u.update(status="Update-Server nicht erreichbar")
        await broadcast({"type": "update", "update": u})
        return
    _, remote = await git("rev-parse", "@{u}")
    u.update(available=local != remote, remote=remote[:7], checked=time.time(),
             status="Neue Version verfügbar" if local != remote else "Aktuell",
             needs_install=NEEDS_INSTALL.exists(), paused=PAUSED.exists())
    if local != remote and PAUSED.exists() and not apply:
        u["status"] = "Neue Version da – automatisch pausiert (zurückgestuft)"
    await broadcast({"type": "update", "update": u})
    if local != remote and (apply or (CFG.get("auto_update", True) and not PAUSED.exists())):
        PAUSED.unlink(missing_ok=True)
        _, changed = await git("diff", "--name-only", local, remote)
        rc, msg = await git("reset", "--hard", "--quiet", remote)
        if rc != 0:
            u.update(status=f"Update fehlgeschlagen: {msg[:60]}")
            await broadcast({"type": "update", "update": u})
            return
        if "install.sh" in changed.split():
            NEEDS_INSTALL.write_text("1")
        log.info("Update auf %s installiert – Neustart", remote[:7])
        await broadcast({"type": "update", "update": {**u, "status": "Update wird installiert ..."}})
        await asyncio.sleep(1.5)
        os._exit(0)          # systemd (Restart=always) startet die neue Version


async def rollback():
    """Eine Version zurück (nach Versions-Markierung), Auto-Update bis zur nächsten Taste pausieren."""
    u = state["update"]
    if not (BASE / ".git").exists():
        return
    # Jede Version ändert CHANGELOG.json -> diese Stände sind die Versionen
    _, head = await git("rev-parse", "HEAD")
    _, hist = await git("log", "--format=%H", "HEAD", "--", "CHANGELOG.json")
    hs = hist.split()
    if hs and hs[0] == head:
        target = hs[1] if len(hs) > 1 else "HEAD~1"
    elif hs:
        target = hs[0]
    else:
        target = "HEAD~1"
    rc, msg = await git("reset", "--hard", "--quiet", target)
    if rc != 0:
        u.update(status=f"Zurückstufen fehlgeschlagen: {msg[:60]}")
        await broadcast({"type": "update", "update": u})
        return
    PAUSED.write_text(target)
    log.info("Auf %s zurückgestuft – Neustart", target)
    await broadcast({"type": "update", "update": {**u, "status": f"Wechsle zu {target} ..."}})
    await asyncio.sleep(1.5)
    os._exit(0)


async def reboot_pi():
    """Pi neu starten (Rechte dafür richtet install.sh ein)."""
    for cmd in (["sudo", "-n", "/usr/sbin/reboot"], ["sudo", "-n", "/sbin/reboot"], ["systemctl", "reboot"]):
        if not shutil.which(cmd[0]):
            continue
        try:
            p = await asyncio.create_subprocess_exec(*cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
            await asyncio.wait_for(p.communicate(), 15)
            if p.returncode == 0:
                return True
        except Exception:
            pass
    log.warning("Neustart nicht erlaubt – install.sh erneut ausführen")
    return False


async def update_loop():
    await asyncio.sleep(30)
    while True:
        try:
            await check_update(apply=False)
        except Exception as e:
            log.warning("Update-Prüfung: %s", e)
        await asyncio.sleep(max(60, int(CFG.get("update_interval_min", 5)) * 60))


# ---------------------------------------------------------------------------
# Web / WebSocket
# ---------------------------------------------------------------------------
def load_changelog():
    try:
        return json.loads((BASE / "CHANGELOG.json").read_text(encoding="utf-8"))
    except Exception:
        return []


def status_msg():
    online = time.time() - state["last_msg"] < 60
    return {"type": "status", "cerbo": state["cerbo"] if online or state["cerbo"] != "Verbunden" else "Keine Daten",
            "online": online, "portal_id": state["portal_id"], "shr": state["shr"],
            "shr_enabled": CFG["shr"]["enabled"], "vehicle": CFG["vehicle"]["name"],
            "wheelbase": CFG["vehicle"].get("wheelbase_m", 4.75), "track": CFG["vehicle"].get("track_m", 2.0),
            "lithium_cfg": [b.get("name") for b in CFG.get("lithium", {}).get("batteries", [])],
            "lithium_addr": [b.get("address", "") for b in CFG.get("lithium", {}).get("batteries", [])],
            "version": state["update"].get("version", ""),
            "app_version": (load_changelog() or [{}])[0].get("version", ""),
            "screen_timeout_min": CFG.get("screen_timeout_min", 0), "ntfy_topic": CFG.get("alarm", {}).get("ntfy_topic", "")}


async def broadcast(msg):
    if not clients:
        return
    data = json.dumps(msg, separators=(",", ":"))
    for ws in list(clients):
        try:
            await ws.send_str(data)
        except Exception:
            clients.discard(ws)


async def push_status():
    await broadcast(status_msg())


async def delta_loop():
    n = 0
    while True:
        await asyncio.sleep(0.4)
        with lock:
            d = dict(changed)
            changed.clear()
        if d:
            await broadcast({"type": "delta", "values": d})
        n += 1
        if n % 5 == 0:
            await push_status()


async def ws_handler(request):
    ws = web.WebSocketResponse(heartbeat=30)
    await ws.prepare(request)
    clients.add(ws)
    with lock:
        snap = dict(values)
    await ws.send_str(json.dumps({"type": "snapshot", "values": snap}))
    await ws.send_str(json.dumps(status_msg()))
    if state["weather"]:
        await ws.send_str(json.dumps({"type": "weather", "weather": state["weather"], "place": state["weather_place"]}))
    await ws.send_str(json.dumps({"type": "lithium", "lithium": state["lithium"]}))
    await ws.send_str(json.dumps({"type": "alarms", "alarms": load_alarms()}))
    await ws.send_str(json.dumps({"type": "ac", "ac": state["ac"]}))
    await ws.send_str(json.dumps({"type": "guard", "guard": guard_public()}))
    await ws.send_str(json.dumps({"type": "update", "update": state["update"]}))
    await ws.send_str(json.dumps({"type": "changelog", "changelog": load_changelog()[:10]}))
    if state["level"]:
        await ws.send_str(json.dumps({"type": "level", "level": state["level"]}))
    async for m in ws:
        if m.type != aiohttp.WSMsgType.TEXT:
            continue
        try:
            cmd = json.loads(m.data)
        except ValueError:
            continue
        t = cmd.get("type")
        if t == "write":
            cerbo_write(str(cmd.get("path", "")), cmd.get("value"))
        elif t == "shr" and cmd.get("cmd") in ("level", "retract"):
            asyncio.create_task(shr_send(cmd["cmd"]))
        elif t == "reboot":
            await broadcast({"type": "reboot_result", "ok": True, "msg": "Der Pi startet neu ..."})
            await asyncio.sleep(1)
            if not await reboot_pi():
                await ws.send_str(json.dumps({"type": "reboot_result", "ok": False,
                    "msg": "Neustart nicht erlaubt – bitte einmal „bash install.sh“ ausführen"}))
        elif t == "screen_timeout":
            CFG["screen_timeout_min"] = max(0, int(cmd.get("value", 0)))
            save_config()
            await push_status()
        elif t == "rollback":
            asyncio.create_task(rollback())
        elif t == "update_check":
            asyncio.create_task(check_update(apply=bool(cmd.get("apply"))))
        elif t == "fullscreen":
            # Vollbild einschalten geht immer, verlassen nur mit PIN
            if cmd.get("enter") or str(cmd.get("pin", "")) == str(CFG.get("fullscreen_pin", "1234")):
                await ws.send_str(json.dumps({"type": "pin_result", "ok": True}))
                await toggle_fullscreen()
            else:
                await ws.send_str(json.dumps({"type": "pin_result", "ok": False}))
        elif t == "brightness":
            set_brightness(float(cmd.get("value", 80)))
        elif t == "bat_scan":
            asyncio.create_task(bat_scan())
        elif t == "bat_assign":
            await bat_assign(int(cmd.get("slot", -1)), str(cmd.get("address", "")), cmd.get("name"))
        elif t == "guard":
            await guard_set(bool(cmd.get("armed")))
        elif t == "guard_test":
            topic = CFG.get("alarm", {}).get("ntfy_topic")
            if not topic:
                res = (False, "Kein Push-Kanal eingetragen")
            else:
                res = await push("Womo: Test", "Die Push-Nachricht funktioniert.", "default", detail=True)
            await ws.send_str(json.dumps({"type": "push_result", "ok": res[0], "msg": res[1], "topic": topic}))
        elif t == "ac_set":
            asyncio.create_task(gree_set(cmd))
        elif t == "level_cal":
            level_calibrate()
        elif t == "alarms" and isinstance(cmd.get("alarms"), list):
            ALARM_FILE.write_text(json.dumps(cmd["alarms"][:20]))
            await broadcast({"type": "alarms", "alarms": cmd["alarms"][:20]})
    clients.discard(ws)
    return ws


async def index(request):
    return web.FileResponse(BASE / "static" / "index.html",
                            headers={"Cache-Control": "no-store, must-revalidate"})


async def on_startup(app):
    try:
        state["update"]["version"] = subprocess.run(["git", "rev-parse", "--short=7", "HEAD"], cwd=BASE,
                                                    capture_output=True, text=True, timeout=10).stdout.strip()
    except Exception:
        pass
    mq.on_connect = on_connect
    mq.on_disconnect = on_disconnect
    mq.on_message = on_message
    mq.reconnect_delay_set(2, 30)
    mq.connect_async(CFG["cerbo"]["host"], int(CFG["cerbo"]["port"]), keepalive=30)
    mq.loop_start()
    for coro in (keepalive_loop(), weather_loop(), delta_loop(), lithium_loop(), level_loop(), gree_loop(), update_loop()):
        asyncio.create_task(coro)


def main():
    app = web.Application()
    app.router.add_get("/", index)
    app.router.add_get("/ws", ws_handler)
    app.router.add_static("/static", BASE / "static")
    app.on_startup.append(on_startup)
    web.run_app(app, host="0.0.0.0", port=int(CFG.get("web_port", 8080)))


if __name__ == "__main__":
    main()
