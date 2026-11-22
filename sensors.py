"""Bluetooth-Sensoren und Kameras.

Bluetooth (nur mithören, keine Verbindung nötig):
- Gasflaschen: Mopeka Pro Check (Ultraschall unter der Flasche)
- Reifendruck: Bluetooth-Ventilkappen vom Typ "TPMS1_xxxxxx" (Hersteller-ID 0x0100)
- Shelly BLU und andere BTHome-v2-Sensoren: Tür/Fenster, Bewegung, Wasser (Leck), Temperatur

Kameras: Reolink-Standbild (wird über den Pi geholt, das Passwort bleibt auf dem Pi).
"""
import asyncio
import json
import time
import uuid

import aiohttp

S = {}      # von server.py: CFG, broadcast, save_config, log, push, ble_lock, BleakScanner, state

SENS = {}           # Adresse -> {"kind","name","rssi","t","data"}
SCAN_STATE = {"busy": False, "last": 0}
BTHOME_UUID = "0000fcd2-0000-1000-8000-00805f9b34fb"

ROLES = {   # Rolle -> Anzeigename
    "gas1": "Gasflasche 1", "gas2": "Gasflasche 2",
    "tpms_vl": "Reifen vorne links", "tpms_vr": "Reifen vorne rechts", "tpms_hl": "Reifen hinten links", "tpms_hr": "Reifen hinten rechts",
    "door_entry": "Eingangstür", "door_cab": "Fahrerhaus-Tür", "door_garage": "Heckgarage", "door_service": "Serviceklappe",
    "win_roof1": "Dachluke vorne", "win_roof2": "Dachluke hinten", "win_sitz": "Fenster Sitzgruppe", "win_bed": "Fenster Schlafraum",
    "leak1": "Wassermelder Boiler", "leak2": "Wassermelder Pumpe", "motion": "Bewegungsmelder", "temp": "Temperaturfühler",
}


def setup(**kw):
    S.update(kw)


def log():
    return S["log"]


def cfg():
    c = S["CFG"].setdefault("sensors", {})
    c.setdefault("devices", [])            # [{"address","role"}]
    c.setdefault("gas_full_mm", 380)       # 11-kg-Flasche voll (ungefähr)
    c.setdefault("tpms_min_bar", 3.5)
    c.setdefault("tpms_drop_bar", 0.4)
    c.setdefault("tpms_nominal", {})
    c.setdefault("cameras", [])            # [{"id","name","host","user","password","channel"}]
    return c


def role_of(addr):
    a = addr.upper()
    for d in cfg()["devices"]:
        if d["address"].upper() == a:
            return d["role"]
    return None


def addr_of(role):
    for d in cfg()["devices"]:
        if d["role"] == role:
            return d["address"].upper()
    return None


# ---------------------------------------------------------------------------
# Auswerten der Funkpakete
# ---------------------------------------------------------------------------
BTH = {  # id: (Name, Bytes, Faktor, signed)
    0x00: ("packet", 1, 1, False), 0x01: ("battery", 1, 1, False), 0x02: ("temperature", 2, .01, True),
    0x03: ("humidity", 2, .01, False), 0x05: ("illuminance", 3, .01, False), 0x0C: ("voltage", 2, .001, False),
    0x0F: ("generic", 1, 1, False), 0x10: ("power", 1, 1, False), 0x11: ("opening", 1, 1, False),
    0x14: ("moisture_pct", 2, .01, False), 0x15: ("battery_low", 1, 1, False), 0x1A: ("door", 1, 1, False),
    0x20: ("moisture", 1, 1, False), 0x21: ("motion", 1, 1, False), 0x2D: ("window", 1, 1, False),
    0x2E: ("humidity", 1, 1, False), 0x3A: ("button", 1, 1, False), 0x3F: ("rotation", 2, .1, True),
    0x45: ("temperature", 2, .1, True), 0x2F: ("humidity", 1, 1, False),
}


def parse_bthome(b: bytes):
    if not b or b[0] & 0x01:              # verschlüsselt -> nicht lesbar
        return None
    out, i = {}, 1
    while i < len(b):
        oid = b[i]
        if oid not in BTH:
            break
        name, n, f, sig = BTH[oid]
        if i + 1 + n > len(b):
            break
        v = int.from_bytes(b[i + 1:i + 1 + n], "little", signed=sig) * f
        out[name] = round(v, 2) if isinstance(v, float) else v
        i += 1 + n
    return out or None


def parse_mopeka(b: bytes):
    if len(b) != 10:
        return None
    temp = (b[2] & 0x7F) - 40
    raw = ((b[4] << 8) + b[3]) & 0x3FFF
    quality = b[4] >> 6
    mm = raw * (0.573045 + (-0.002822 * temp) + (-0.00000535 * temp * temp))
    full = float(cfg()["gas_full_mm"]) or 380
    return {"battery_v": round((b[1] & 0x7F) / 32, 2), "temperature": temp, "level_mm": round(mm),
            "quality": quality, "percent": max(0, min(100, round(mm / full * 100)))}


def parse_tpms(b: bytes):
    if len(b) != 16:
        return None
    kpa = int.from_bytes(b[6:10], "little") / 1000
    temp = int.from_bytes(b[10:14], "little", signed=True) / 100
    if not 0 <= kpa < 1500 or not -40 < temp < 120:
        return None
    return {"bar": round(kpa / 100, 2), "temperature": round(temp, 1), "battery": b[14], "alarm": b[15]}


def on_adv(device, adv):
    addr = device.address.upper()
    now = time.time()
    kind = data = None
    sd = adv.service_data or {}
    if BTHOME_UUID in sd:
        data, kind = parse_bthome(sd[BTHOME_UUID]), "bthome"
    md = adv.manufacturer_data or {}
    if not data and 0x0059 in md and len(md[0x0059]) == 10 and (md[0x0059][0] & 0xCF) in (3, 4, 5, 6, 8, 9, 10, 11):
        data, kind = parse_mopeka(md[0x0059]), "mopeka"
    if not data and 0x0100 in md:
        data, kind = parse_tpms(md[0x0100]), "tpms"
    if not data:
        return
    name = adv.local_name or device.name or ""
    old = SENS.get(addr, {})
    SENS[addr] = {"kind": kind, "name": name, "rssi": adv.rssi, "t": now, "data": {**old.get("data", {}), **data}}
    if kind == "bthome":
        try:
            asyncio.ensure_future(contact_event(addr, old.get("data", {}), data))
        except RuntimeError:
            pass


async def contact_event(addr, old, new):
    """Tür/Fenster geöffnet während der Bewegungsalarm scharf ist, Wasser erkannt."""
    role = role_of(addr)
    if not role:
        return
    label = ROLES.get(role, role)
    opened = any(new.get(k) == 1 and old.get(k) != 1 for k in ("window", "door", "opening"))
    guard = (S["state"].get("guard") or {})
    if opened and guard.get("armed"):
        await S["push"]("🚨 ALARM – Wohnmobil", f"{label} wurde geöffnet ({time.strftime('%H:%M:%S')} Uhr)!", "urgent")
    if new.get("motion") == 1 and old.get("motion") != 1 and guard.get("armed"):
        await S["push"]("🚨 ALARM – Wohnmobil", f"{label}: Bewegung erkannt ({time.strftime('%H:%M:%S')} Uhr)!", "urgent")


# ---------------------------------------------------------------------------
# Zusammenfassungen (Warnungen, Sprache, Abfahrt)
# ---------------------------------------------------------------------------
def fresh(addr, maxage=1800):
    s = SENS.get(addr or "")
    return s if s and time.time() - s["t"] < maxage else None


def gas_levels():
    out = []
    for r in ("gas1", "gas2"):
        s = fresh(addr_of(r))
        if s:
            out.append((ROLES[r], s["data"].get("percent")))
    return out


def tpms_values():
    out = {}
    for r in ("tpms_vl", "tpms_vr", "tpms_hl", "tpms_hr"):
        s = fresh(addr_of(r), 3600)
        if s:
            out[r] = s["data"]
    return out


def open_contacts():
    res = []
    for d in cfg()["devices"]:
        s = fresh(d["address"].upper(), 6 * 3600)
        if s and s["kind"] == "bthome" and any(s["data"].get(k) == 1 for k in ("window", "door", "opening")):
            res.append(ROLES.get(d["role"], d["role"]))
    return res


def gas_summary():
    g = gas_levels()
    if not g:
        return None
    return " ".join(f"{n}: {p} Prozent." for n, p in g if p is not None)


def tpms_summary():
    t = tpms_values()
    if not t:
        return None
    return " ".join(f"{ROLES[r].replace('Reifen ', '')} {str(d['bar']).replace('.', ',')} bar." for r, d in t.items())


def checks(active):
    """Für die Warnungen (extras): {id: (aktiv, titel, text)}"""
    c, res = cfg(), {}
    g = [(n, p) for n, p in gas_levels() if p is not None]
    if g:
        low = [(n, p) for n, p in g if p <= (25 if active("gas") else 20)]
        res["gas"] = (bool(low) and len(low) == len(g), "🔥 Gas fast leer",
                      " ".join(f"{n}: {p} %." for n, p in g) + " Bald Flasche wechseln.")
    t = tpms_values()
    if t:
        bad = []
        for r, d in t.items():
            nom = c["tpms_nominal"].get(r)
            lim = max(c["tpms_min_bar"], (nom - c["tpms_drop_bar"]) if nom else 0)
            if d["bar"] < lim + (0.1 if active("tpms") else 0) or d.get("alarm"):
                bad.append(f"{ROLES[r].replace('Reifen ', '')}: {str(d['bar']).replace('.', ',')} bar")
        res["tpms"] = (bool(bad), "🛞 Reifendruck zu niedrig", ", ".join(bad) + ". Bitte prüfen!")
    leaks = []
    for d in cfg()["devices"]:
        s = fresh(d["address"].upper(), 6 * 3600)
        if s and s["data"].get("moisture") == 1:
            leaks.append(ROLES.get(d["role"], d["role"]))
    res["leak"] = (bool(leaks), "💦 Wasser erkannt!", ", ".join(leaks) + ": Feuchtigkeit gemessen – bitte nachsehen.")
    return res


# ---------------------------------------------------------------------------
# Kameras (Reolink)
# ---------------------------------------------------------------------------
async def camera_jpeg(cid):
    cam = next((c for c in cfg()["cameras"] if c["id"] == cid), None)
    if not cam:
        return None, "unbekannt"
    url = (f"http://{cam['host']}/cgi-bin/api.cgi?cmd=Snap&channel={int(cam.get('channel') or 0)}&rs={uuid.uuid4().hex[:8]}"
           f"&user={cam.get('user') or 'admin'}&password={cam.get('password') or ''}")
    try:
        async with aiohttp.ClientSession() as s:
            async with s.get(url, timeout=aiohttp.ClientTimeout(total=6)) as r:
                body = await r.read()
                if r.status == 200 and body[:2] == b"\xff\xd8":
                    return body, None
                return None, f"Kamera antwortet nicht mit Bild (HTTP {r.status})"
    except Exception as e:
        return None, f"Kamera nicht erreichbar ({type(e).__name__})"


def cams_public():
    return [{"id": c["id"], "name": c["name"], "host": c["host"], "user": c.get("user", "admin"), "channel": c.get("channel", 0)}
            for c in cfg()["cameras"]]


# ---------------------------------------------------------------------------
# Oberfläche
# ---------------------------------------------------------------------------
def sensors_msg():
    now = time.time()
    found = []
    for a, s in sorted(SENS.items(), key=lambda x: -x[1]["t"]):
        if now - s["t"] > 3600:
            continue
        found.append({"address": a, "kind": s["kind"], "name": s["name"], "rssi": s["rssi"], "age": int(now - s["t"]),
                      "data": s["data"], "role": role_of(a)})
    c = cfg()
    return {"type": "sensors", "found": found, "roles": ROLES, "devices": c["devices"], "gas_full_mm": c["gas_full_mm"],
            "tpms_min_bar": c["tpms_min_bar"], "tpms_nominal": c["tpms_nominal"], "cams": cams_public(),
            "ble": S.get("BleakScanner") is not None}


async def handle(cmd, ws):
    t = cmd.get("type", "")
    c = cfg()
    if t == "sens_get":
        await ws.send_str(json.dumps(sensors_msg()))
        return True
    if t == "sens_assign":
        a, role = str(cmd.get("address", "")).upper(), cmd.get("role")
        c["devices"] = [d for d in c["devices"] if d["address"].upper() != a and d["role"] != role]
        if role in ROLES:
            c["devices"].append({"address": a, "role": role})
    elif t == "sens_unassign":
        c["devices"] = [d for d in c["devices"] if d["address"].upper() != str(cmd.get("address", "")).upper()]
    elif t == "gas_full":             # jetzt volle Flasche -> Messwert als "voll" merken
        s = fresh(addr_of(cmd.get("role", "gas1")))
        if s and s["data"].get("level_mm"):
            c["gas_full_mm"] = s["data"]["level_mm"]
    elif t == "tpms_nominal":         # aktuellen Druck als Sollwert merken
        c["tpms_nominal"] = {r: d["bar"] for r, d in tpms_values().items()}
    elif t == "tpms_min":
        c["tpms_min_bar"] = max(1.0, min(8.0, float(c["tpms_min_bar"]) + float(cmd.get("delta", 0))))
    elif t == "cam_add":
        if str(cmd.get("host", "")).strip():
            c["cameras"].append({"id": uuid.uuid4().hex[:6], "name": str(cmd.get("name") or "Kamera")[:30],
                                 "host": str(cmd["host"]).strip()[:60], "user": str(cmd.get("user") or "admin")[:30],
                                 "password": str(cmd.get("password") or "")[:60], "channel": int(cmd.get("channel") or 0)})
    elif t == "cam_del":
        c["cameras"] = [x for x in c["cameras"] if x["id"] != cmd.get("id")]
    else:
        return False
    S["save_config"]()
    await S["broadcast"](sensors_msg())
    return True


async def cam_route(request):
    from aiohttp import web
    body, err = await camera_jpeg(request.match_info.get("cid", ""))
    if body is None:
        return web.Response(status=502, text=err or "Fehler")
    return web.Response(body=body, content_type="image/jpeg", headers={"Cache-Control": "no-store"})


# ---------------------------------------------------------------------------
# Mithören (alle 30 s für 8 s, abwechselnd mit den LiTime-Akkus)
# ---------------------------------------------------------------------------
async def scan_loop():
    Scanner = S.get("BleakScanner")
    if Scanner is None:
        log().info("Bluetooth nicht verfügbar – Bluetooth-Sensoren aus")
        return
    await asyncio.sleep(20)
    last_sent = 0
    while True:
        try:
            async with S["ble_lock"]:
                SCAN_STATE["busy"] = True
                sc = Scanner(detection_callback=on_adv)
                await sc.start()
                await asyncio.sleep(8)
                await sc.stop()
        except Exception as e:
            log().info("Bluetooth-Sensoren: %s", e)
        finally:
            SCAN_STATE["busy"] = False
        if SENS and time.time() - last_sent > 25:
            last_sent = time.time()
            await S["broadcast"](sensors_msg())
        await asyncio.sleep(22)


def start():
    cfg()
    asyncio.get_running_loop().create_task(scan_loop())
