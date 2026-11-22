"""Zusatzfunktionen der Womo-Zentrale.

- Warnungen aufs Handy (Akku, Tanks, Landstrom, Kühlschrank, Frost, Sturm)
- Verlauf (Akku, Solar, Verbrauch) und Tagesbilanz
- Akku-Prognose ("reicht noch ...") mit Solar-Vorhersage
- Abfahrts-Checkliste mit Warnung beim Losfahren
- Reisetagebuch (Strecke und Stellplätze)
- Wartungs-Erinnerungen
- Zugriff vom Handy (Adressen, Fernzugang)

server.py ruft setup(...) mit seinen Objekten auf und startet loops().
"""
import asyncio
import datetime as dt
import json
import math
import socket
import subprocess
import time
import uuid

import aiohttp

S = {}          # von server.py: CFG, values, state, push, broadcast, save_config, log, BASE, location, lock


def setup(**kw):
    S.update(kw)


def log():
    return S["log"]


# ---------------------------------------------------------------------------
# Hilfen
# ---------------------------------------------------------------------------
def num(x):
    try:
        if x is None or isinstance(x, bool):
            return None
        f = float(x)
        return f if math.isfinite(f) else None
    except (TypeError, ValueError):
        return None


def val(key):
    return num(S["values"].get(key))


def instances(service):
    out = set()
    for k in list(S["values"].keys()):
        p = k.split("/")
        if len(p) > 2 and p[0] == service:
            out.add(p[1])
    return sorted(out, key=lambda x: int(x) if x.isdigit() else 0)


def sval(service, inst, path):
    return S["values"].get(f"{service}/{inst}/{path}")


def dev_name(service, inst):
    return str(sval(service, inst, "CustomName") or sval(service, inst, "ProductName") or "")


def jload(name, default):
    try:
        return json.loads((S["BASE"] / name).read_text(encoding="utf-8"))
    except Exception:
        return default


def jsave(name, data):
    p = S["BASE"] / name
    tmp = p.with_suffix(p.suffix + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    tmp.replace(p)


def cerbo_online():
    return time.time() - S["state"].get("last_msg", 0) < 60


def today():
    return dt.date.today()


def km_between(a, b):
    """Entfernung in km zwischen (lat, lon)-Paaren."""
    la1, lo1, la2, lo2 = map(math.radians, (a[0], a[1], b[0], b[1]))
    h = math.sin((la2 - la1) / 2) ** 2 + math.cos(la1) * math.cos(la2) * math.sin((lo2 - lo1) / 2) ** 2
    return 12742 * math.asin(min(1, math.sqrt(h)))


def soc_now():
    soc = val("system/0/Dc/Battery/Soc")
    if soc is not None:
        return soc
    # sonst Mittelwert der LiTime-Akkus (Bluetooth)
    socs = []
    for b in (S["state"].get("lithium") or {}).values():
        d = b.get("data") or {}
        for k, v in d.items():
            if "soc" in k.lower() and num(v) is not None:
                socs.append(num(v))
                break
    return sum(socs) / len(socs) if socs else None


def shore_on():
    src = val("system/0/Ac/ActiveIn/Source")
    return src in (1, 2, 3)


def power_now():
    pv = val("system/0/Dc/Pv/Power")
    acl = val("system/0/Ac/Consumption/L1/Power")
    dcl = val("system/0/Dc/System/Power")
    load = None if acl is None and dcl is None else (acl or 0) + max(0, dcl or 0)
    grid = None
    for k in ("system/0/Ac/Grid/L1/Power", "system/0/Ac/Genset/L1/Power"):
        if val(k) is not None:
            grid = (grid or 0) + val(k)
    return {"pv": pv, "load": load, "bat": val("system/0/Dc/Battery/Power"), "grid": grid}


def temp_by_name(pattern, exclude=None):
    import re
    rx, ex = re.compile(pattern, re.I), re.compile(exclude, re.I) if exclude else None
    for i in instances("temperature"):
        n = dev_name("temperature", i)
        if rx.search(n) and not (ex and ex.search(n)):
            t = num(sval("temperature", i, "Temperature"))
            if t is not None:
                return n, t
    return None, None


def tank_level(fluids):
    for i in instances("tank"):
        if num(sval("tank", i, "FluidType")) in fluids:
            lv = num(sval("tank", i, "Level"))
            if lv is not None:
                return dev_name("tank", i), lv
    return None, None


# ---------------------------------------------------------------------------
# Warnungen
# ---------------------------------------------------------------------------
WARN_DEFAULTS = {
    "enabled": True, "soc_low": 20, "fresh_low": 10, "grey_high": 85, "black_high": 85,
    "shore_lost": True, "freezer_max": -10, "fridge_max": 10, "frost_temp": 1, "gust_kmh": 60,
    "off": [],
}
WARN_IDS = ["soc", "soc_crit", "fresh", "grey", "black", "shore", "freezer", "fridge", "frost", "storm", "gas", "tpms", "leak"]
WARN_NAMES = {"soc": "Akku niedrig", "soc_crit": "Akku fast leer", "fresh": "Frischwasser fast leer",
              "grey": "Grauwasser fast voll", "black": "Toilette fast voll", "shore": "Landstrom ausgefallen",
              "freezer": "Gefrierfach zu warm", "fridge": "Kühlschrank zu warm", "frost": "Frostwarnung",
              "storm": "Sturmwarnung", "gas": "Gas fast leer", "tpms": "Reifendruck zu niedrig", "leak": "Wassermelder"}
# wie viele Prüfungen (je 30 s) eine Bedingung anstehen muss, bevor gewarnt wird
WARN_HOLD = {"freezer": 20, "fridge": 20, "soc": 4, "soc_crit": 2, "fresh": 4, "grey": 4, "black": 4, "shore": 4}
WARN_PRIO = {"leak": "urgent", "tpms": "urgent", "soc_crit": "urgent", "shore": "high", "storm": "high", "freezer": "high", "soc": "high"}

WARN = {}               # id -> {"title", "text", "since", "pushed"}
_warn_count = {}
_shore = {"on_since": None}


def wcfg():
    c = dict(WARN_DEFAULTS)
    c.update(S["CFG"].get("warnings") or {})
    return c


def warn_list():
    out = []
    for wid in WARN_IDS:
        w = WARN.get(wid)
        if w:
            out.append({"id": wid, "title": w["title"], "text": w["text"], "since": w["since"]})
    return out


def _check_conditions():
    """Liefert {id: (aktiv, titel, text)} für alle Prüfungen."""
    c, st, res = wcfg(), S["state"], {}
    active = lambda k: k in WARN
    online = cerbo_online()

    soc = soc_now() if online or S["state"].get("lithium") else None
    if soc is not None and c["soc_low"]:
        lim = c["soc_low"] + (5 if active("soc") else 0)
        res["soc"] = (soc <= lim, "🔋 Akku niedrig",
                      f"Die Aufbaubatterie hat nur noch {soc:.0f} %. Bitte Strom sparen oder laden.")
        lim2 = max(5, c["soc_low"] / 2) + (3 if active("soc_crit") else 0)
        res["soc_crit"] = (soc <= lim2, "🪫 Akku fast leer!",
                           f"Die Aufbaubatterie ist bei {soc:.0f} % – gleich schalten sich die Verbraucher ab.")
    if online:
        n, lv = tank_level([1])
        if lv is not None and c["fresh_low"]:
            res["fresh"] = (lv <= c["fresh_low"] + (5 if active("fresh") else 0), "💧 Frischwasser fast leer",
                            f"{n or 'Frischwasser'}: nur noch {lv:.0f} %.")
        n, lv = tank_level([2])
        if lv is not None and c["grey_high"]:
            res["grey"] = (lv >= c["grey_high"] - (5 if active("grey") else 0), "🚿 Grauwasser fast voll",
                           f"{n or 'Grauwasser'}: {lv:.0f} % – bald entleeren.")
        n, lv = tank_level([5])
        if lv is not None and c["black_high"]:
            res["black"] = (lv >= c["black_high"] - (5 if active("black") else 0), "🚽 Toilette fast voll",
                            f"{n or 'Toilette'}: {lv:.0f} % – bald entleeren.")
        # Landstrom: nur melden, wenn er vorher mindestens 2 Minuten anlag – und nicht, wenn das Kabel
        # laut Abfahrts-Checkliste bewusst abgesteckt wurde oder das Womo fährt
        if c["shore_lost"]:
            unplugged = DRIVE["moving"] or any(x["done"] and "strom" in x["t"].lower() for x in CHECK["items"])
            if shore_on():
                _shore["on_since"] = _shore["on_since"] or time.time()
                res["shore"] = (False, "", "")
            elif val("system/0/Ac/ActiveIn/Source") is not None:
                was = bool(_shore["on_since"]) and time.time() - _shore["on_since"] > 120
                res["shore"] = ((was or active("shore")) and not unplugged, "🔌 Landstrom weg",
                                "Der Landstrom ist ausgefallen oder getrennt – das Womo läuft jetzt über die Batterie.")
                if unplugged:
                    _shore["on_since"] = None
        n, t = temp_by_name(r"gefrier|tiefk|freez")
        if t is not None and c["freezer_max"] is not None:
            res["freezer"] = (t > c["freezer_max"] - (2 if active("freezer") else 0), "🧊 Gefrierfach zu warm",
                              f"{n}: {t:.1f} °C (Grenze {c['freezer_max']} °C).")
        n, t = temp_by_name(r"kühl|kuehl|fridge", r"gefrier|tiefk|freez")
        if t is not None and c["fridge_max"] is not None:
            res["fridge"] = (t > c["fridge_max"] - (1 if active("fridge") else 0), "🥶 Kühlschrank zu warm",
                             f"{n}: {t:.1f} °C (Grenze {c['fridge_max']} °C).")

    if S.get("sensor_checks"):
        try:
            res.update(S["sensor_checks"](active))
        except Exception as e:
            log().info("Sensor-Warnungen: %s", e)
    wx = st.get("weather") or {}
    h = wx.get("hourly") or {}
    if h.get("time"):
        now = time.time()
        idx = [k for k, t in enumerate(h["time"]) if _ts(t) >= now - 3600]
        nxt12, nxt6 = idx[:12], idx[:6]
        temps = [(h["temperature_2m"][k], h["time"][k]) for k in nxt12 if h["temperature_2m"][k] is not None]
        if temps and c["frost_temp"] is not None:
            tmin, tt = min(temps)
            lim = c["frost_temp"] + (1 if active("frost") else 0)
            res["frost"] = (tmin <= lim, "❄️ Frost erwartet",
                            f"Bis {tt[11:16]} Uhr sinkt die Temperatur auf {tmin:.0f} °C. Wasserleitungen und "
                            f"Heizung prüfen (Frostwächter, Wasser ablassen).")
        gusts = [(h["wind_gusts_10m"][k], h["time"][k]) for k in nxt6 if h["wind_gusts_10m"][k] is not None]
        cur = (wx.get("current") or {}).get("wind_gusts_10m")
        if cur is not None:
            gusts.append((cur, "jetzt"))
        if gusts and c["gust_kmh"]:
            gmax, gt = max(gusts, key=lambda x: x[0])
            lim = c["gust_kmh"] - (10 if active("storm") else 0)
            when = "jetzt" if gt == "jetzt" else f"um {gt[11:16]} Uhr"
            res["storm"] = (gmax >= lim, "🌬️ Sturmböen erwartet",
                            f"Böen bis {gmax:.0f} km/h ({when}). Markise einfahren, Sat-Schüssel und lose Teile sichern.")
    return res


def _ts(iso):
    try:
        return dt.datetime.fromisoformat(iso).timestamp()
    except Exception:
        return 0


async def warnings_tick():
    c = wcfg()
    res = _check_conditions() if c["enabled"] else {}
    changed = False
    for wid in WARN_IDS:
        on, title, text = res.get(wid, (False, "", ""))
        if wid in c.get("off", []):
            on = False
        if on:
            _warn_count[wid] = _warn_count.get(wid, 0) + 1
            if wid in WARN:
                if WARN[wid]["text"] != text:
                    WARN[wid]["text"] = text
                    changed = True
            elif _warn_count[wid] >= WARN_HOLD.get(wid, 1):
                WARN[wid] = {"title": title, "text": text, "since": time.time(), "pushed": False}
                changed = True
                log().warning("Warnung: %s – %s", title, text)
                if c.get("push", True):
                    ok = await S["push"](title, text, WARN_PRIO.get(wid, "default"))
                    WARN[wid]["pushed"] = ok
        else:
            _warn_count[wid] = 0
            if wid in WARN:
                del WARN[wid]
                changed = True
    if changed:
        await S["broadcast"]({"type": "warnings", "list": warn_list()})


# ---------------------------------------------------------------------------
# Verlauf und Tagesbilanz
# ---------------------------------------------------------------------------
HIST = {"pts": [], "days": {}}          # pts: [t, soc, pv, load, bat, grid]
_last_sample = {"t": None}


def hist_load():
    d = jload("history.json", {})
    HIST["pts"] = d.get("pts", [])
    HIST["days"] = d.get("days", {})


def hist_save():
    try:
        jsave("history.json", HIST)
    except Exception as e:
        log().warning("Verlauf nicht gespeichert: %s", e)


def hist_sample():
    now = time.time()
    p = power_now()
    soc = soc_now()
    if not cerbo_online() and soc is None:
        _last_sample["t"] = None
        return
    r = lambda x, n=0: None if x is None else round(x, n)
    HIST["pts"].append([int(now), r(soc, 1), r(p["pv"]), r(p["load"]), r(p["bat"]), r(p["grid"])])
    cutoff = now - 7 * 86400
    while HIST["pts"] and HIST["pts"][0][0] < cutoff:
        HIST["pts"].pop(0)
    # Tagesbilanz (Wh) integrieren
    day = today().isoformat()
    d = HIST["days"].setdefault(day, {"pv_wh": 0, "load_wh": 0, "grid_wh": 0, "min_soc": None, "max_soc": None})
    if _last_sample["t"]:
        h = min(300, now - _last_sample["t"]) / 3600
        d["pv_wh"] = round(d["pv_wh"] + max(0, p["pv"] or 0) * h, 1)
        d["load_wh"] = round(d["load_wh"] + max(0, p["load"] or 0) * h, 1)
        d["grid_wh"] = round(d["grid_wh"] + max(0, p["grid"] or 0) * h, 1)
    if soc is not None:
        d["min_soc"] = soc if d["min_soc"] is None else min(d["min_soc"], soc)
        d["max_soc"] = soc if d["max_soc"] is None else max(d["max_soc"], soc)
    wx = S["state"].get("weather") or {}
    dl = wx.get("daily") or {}
    if dl.get("time") and day in dl["time"] and dl.get("shortwave_radiation_sum"):
        rad = dl["shortwave_radiation_sum"][dl["time"].index(day)]
        if rad is not None:
            d["rad_kwh"] = round(rad / 3.6, 2)          # MJ/m² -> kWh/m²
    _last_sample["t"] = now
    for k in sorted(HIST["days"])[:-60]:
        del HIST["days"][k]


def hist_get(rng):
    span, bucket = (86400, 300) if rng != "7d" else (7 * 86400, 1800)
    now = time.time()
    pts = [p for p in HIST["pts"] if p[0] >= now - span]
    out, cur, acc = [], None, []

    def flush():
        if not acc:
            return
        row = [cur]
        for k in range(1, 6):
            xs = [a[k] for a in acc if a[k] is not None]
            row.append(round(sum(xs) / len(xs), 1) if xs else None)
        out.append(row)

    for p in pts:
        b = p[0] // bucket * bucket
        if b != cur:
            flush()
            cur, acc = b, []
        acc.append(p)
    flush()
    days = [{"day": k, **v} for k, v in sorted(HIST["days"].items())[-14:]]
    return {"type": "history", "range": rng, "bucket": bucket, "pts": out, "days": days}


# ---------------------------------------------------------------------------
# Akku-Prognose
# ---------------------------------------------------------------------------
FORECAST = {}


def battery_wh():
    e = S["CFG"].get("energy") or {}
    if num(e.get("battery_wh")):
        return num(e["battery_wh"]), "eingestellt"
    v = num(e.get("nominal_v")) or 12.8
    for i in instances("battery"):
        ah = num(sval("battery", i, "Capacity")) or num(sval("battery", i, "InstalledCapacity"))
        if ah:
            return ah * v, "Batteriewächter"
    ah = 0
    for b in (S["state"].get("lithium") or {}).values():
        for k, x in (b.get("data") or {}).items():
            kl = k.lower()
            if ("full" in kl or "design" in kl or "nominal" in kl or kl.endswith("capacity")) and "remain" not in kl \
                    and num(x) and 20 <= num(x) <= 600:
                ah += num(x)
                break
    if ah:
        return ah * v, "LiTime-Akkus"
    return 300 * v, "angenommen (3 × 100 Ah)"


def pv_factor():
    """m² "wirksame Fläche": PV-Watt = Faktor × Einstrahlung (W/m²). Aus den Tagesbilanzen gelernt."""
    days = [d for k, d in sorted(HIST["days"].items())[-15:-1]
            if (d.get("rad_kwh") or 0) >= 0.5 and d.get("pv_wh", 0) > 50 and (d.get("max_soc") or 0) < 97]
    if len(days) >= 3:
        f = sum(d["pv_wh"] / 1000 for d in days) / sum(d["rad_kwh"] for d in days)
        return max(0.02, min(3.0, f)), f"aus {len(days)} Tagen gelernt"
    wp = num((S["CFG"].get("energy") or {}).get("pv_wp")) or 400
    return wp * 0.75 / 1000, f"geschätzt ({wp:.0f} Wp)"


def avg_load():
    now = time.time()
    xs = [p[3] for p in HIST["pts"] if p[0] >= now - 86400 and p[3] is not None]
    if len(xs) >= 120:
        return sum(xs) / len(xs), "Ø 24 h"
    cur = power_now()["load"]
    if xs and cur is not None:
        return (sum(xs) + cur) / (len(xs) + 1), "Ø bisher"
    return cur, "aktuell"


def forecast_calc():
    soc = soc_now()
    cap, cap_src = battery_wh()
    load, load_src = avg_load()
    f, f_src = pv_factor()
    res = {"soc": soc, "cap_wh": round(cap), "cap_src": cap_src, "load_w": None if load is None else round(load),
           "load_src": load_src, "pv_src": f_src, "shore": shore_on(), "hours": None, "full_in": None,
           "pv_today_wh": None, "pv_tomorrow_wh": None, "text": "", "t": time.time(),
           "pv_wp": num((S["CFG"].get("energy") or {}).get("pv_wp")) or 400}
    wx = S["state"].get("weather") or {}
    h = wx.get("hourly") or {}
    if soc is None or load is None:
        res["text"] = "Noch keine Daten"
        return res
    rad = h.get("shortwave_radiation") or []
    times = h.get("time") or []
    now = time.time()
    e = soc / 100 * cap
    reserve = cap * (num((S["CFG"].get("energy") or {}).get("min_soc")) or 10) / 100
    hours = 0.0
    empty_at = full_at = None
    day_pv = {}
    for k, t in enumerate(times):
        ts = _ts(t)
        if ts + 3600 <= now:
            continue
        frac = min(1.0, (ts + 3600 - now) / 3600)
        pv = f * (rad[k] or 0) if k < len(rad) else 0
        day_pv[t[:10]] = day_pv.get(t[:10], 0) + pv * frac
        e += (pv - load) * frac
        hours += frac
        if e >= cap:
            e = cap
            if full_at is None and pv > load:
                full_at = hours
        if e <= reserve and empty_at is None:
            empty_at = hours - (reserve - e) / max(1, load - pv) if load > pv else hours
    if not times:          # ohne Wetter nur Verbrauch
        empty_at = max(0, (e - reserve)) / load if load > 1 else None
        hours = empty_at or 0
    res["hours"] = None if empty_at is None else round(max(0, empty_at), 1)
    res["horizon_h"] = round(hours)
    res["full_in"] = None if full_at is None else round(full_at, 1)
    td = today().isoformat()
    tm = (today() + dt.timedelta(days=1)).isoformat()
    res["pv_today_wh"] = round(day_pv.get(td, 0))
    res["pv_tomorrow_wh"] = round(day_pv.get(tm, 0))
    if res["shore"]:
        res["text"] = "Landstrom – Akku wird geladen"
    elif res["hours"] is None:
        res["text"] = f"reicht über {max(1, round(hours / 24))} Tage" if hours > 48 else "Solar deckt den Verbrauch"
    elif res["hours"] < 48:
        res["text"] = f"reicht noch ca. {res['hours']:.0f} Std."
    else:
        res["text"] = "reicht noch ca. " + f"{res['hours'] / 24:.1f}".replace(".", ",") + " Tage"
    return res


# ---------------------------------------------------------------------------
# Abfahrts-Checkliste und Fahrterkennung
# ---------------------------------------------------------------------------
CHECK_DEFAULT = ["Hubstützen eingefahren", "Dachluken und Fenster zu", "Markise eingefahren",
                 "Stromkabel abgesteckt", "Sat-Schüssel eingefahren", "Trittstufe eingefahren",
                 "Gas zu (oder Fahrtbetrieb erlaubt)", "Kühlschrank verriegelt", "Schränke und Türen verriegelt",
                 "Abwasser-Schieber zu", "Keile eingesammelt", "Lose Teile gesichert"]
CHECK = {"items": []}
DRIVE = {"moving": False, "fast": 0, "still_since": None, "alerted": False, "last_pos": None, "last_t": 0}


def check_load():
    d = jload("checklist.json", None)
    CHECK["items"] = d["items"] if d and isinstance(d.get("items"), list) else [{"t": t, "done": False} for t in CHECK_DEFAULT]


def check_save():
    jsave("checklist.json", CHECK)


def check_msg():
    return {"type": "checklist", "items": CHECK["items"], "moving": DRIVE["moving"],
            "gps": S["location"]()["src"] == "gps"}


async def check_cmd(cmd):
    t, it = cmd.get("type"), CHECK["items"]
    i = cmd.get("i")
    if t == "chk_toggle" and isinstance(i, int) and 0 <= i < len(it):
        it[i]["done"] = not it[i]["done"]
    elif t == "chk_reset":
        for x in it:
            x["done"] = False
    elif t == "chk_all":
        for x in it:
            x["done"] = True
    elif t == "chk_add" and str(cmd.get("text", "")).strip():
        it.append({"t": str(cmd["text"]).strip()[:60], "done": False})
    elif t == "chk_del" and isinstance(i, int) and 0 <= i < len(it):
        it.pop(i)
    elif t == "chk_default":
        CHECK["items"] = [{"t": x, "done": False} for x in CHECK_DEFAULT]
    check_save()
    await S["broadcast"](check_msg())


def gps_speed():
    for i in instances("gps"):
        sp = num(sval("gps", i, "Speed"))
        if sp is not None:
            return sp * 3.6          # m/s -> km/h
    return None


async def drive_tick():
    """Alle 10 s: fährt das Womo? Beim Losfahren offene Punkte der Checkliste melden."""
    loc = S["location"]()
    if loc["src"] != "gps":
        return
    now = time.time()
    pos = (loc["lat"], loc["lon"])
    sp = gps_speed()
    if sp is None and DRIVE["last_pos"]:
        dtm = max(1, now - DRIVE["last_t"])
        sp = km_between(DRIVE["last_pos"], pos) / dtm * 3600
    DRIVE["last_pos"], DRIVE["last_t"] = pos, now
    if sp is None:
        return
    if sp > 8:
        DRIVE["fast"] += 1
        DRIVE["still_since"] = None
    else:
        DRIVE["fast"] = 0
        DRIVE["still_since"] = DRIVE["still_since"] or now
    if not DRIVE["moving"] and DRIVE["fast"] >= 2:
        DRIVE["moving"] = True
        open_items = [x["t"] for x in CHECK["items"] if not x["done"]]
        msgs = []
        if shore_on():
            msgs.append("STROMKABEL NOCH EINGESTECKT!")
        contacts = S["open_contacts"]() if S.get("open_contacts") else []
        if contacts:
            msgs.append("OFFEN: " + ", ".join(contacts) + "!")
            open_items = [f"⚠ {c} ist offen" for c in contacts] + open_items
        if open_items:
            msgs.append("Noch offen: " + ", ".join(open_items[:6]) + (" …" if len(open_items) > 6 else ""))
        if msgs and not DRIVE["alerted"]:
            DRIVE["alerted"] = True
            await S["push"]("🚐 Abfahrt – bitte prüfen!", " ".join(msgs), "urgent" if shore_on() else "high")
            await S["broadcast"]({"type": "drive_alert", "open": open_items, "shore": shore_on()})
        log().info("Fahrt erkannt")
        await S["broadcast"](check_msg())
    if DRIVE["moving"] and DRIVE["still_since"] and now - DRIVE["still_since"] > 1800:
        DRIVE["moving"] = False
        DRIVE["alerted"] = False
        log().info("Fahrt beendet")
        await S["broadcast"](check_msg())
    # 2 Stunden nach der Ankunft Haken zurücksetzen – bereit für die nächste Abfahrt
    if (not DRIVE["moving"] and DRIVE["still_since"] and now - DRIVE["still_since"] > 7200
            and (S["CFG"].get("checklist") or {}).get("auto_reset", True) and any(x["done"] for x in CHECK["items"])
            and DRIVE.get("reset_for") != DRIVE["still_since"]):
        DRIVE["reset_for"] = DRIVE["still_since"]
        for x in CHECK["items"]:
            x["done"] = False
        check_save()
        await S["broadcast"](check_msg())


# ---------------------------------------------------------------------------
# Reisetagebuch
# ---------------------------------------------------------------------------
TRIP = {"track": [], "stays": [], "km": {}}
_anchor = {"pos": None, "t": None}


def trip_load():
    d = jload("trip.json", {})
    TRIP["track"] = d.get("track", [])
    TRIP["stays"] = d.get("stays", [])
    TRIP["km"] = d.get("km", {})


def trip_save():
    try:
        jsave("trip.json", TRIP)
    except Exception as e:
        log().warning("Reisetagebuch nicht gespeichert: %s", e)


async def place_name(lat, lon):
    try:
        async with aiohttp.ClientSession(headers={"User-Agent": "Womo-Zentrale/1.0"}) as s:
            async with s.get("https://nominatim.openstreetmap.org/reverse",
                             params={"format": "json", "zoom": "14", "lat": f"{lat:.5f}", "lon": f"{lon:.5f}",
                                     "accept-language": "de"}, timeout=aiohttp.ClientTimeout(total=15)) as r:
                a = (await r.json(content_type=None)).get("address", {})
        town = a.get("city") or a.get("town") or a.get("village") or a.get("municipality") or ""
        cc = (a.get("country_code") or "").upper()
        return ", ".join(x for x in (town, cc if cc and cc != "DE" else "") if x) or "Unbekannter Ort"
    except Exception:
        return "Unbekannter Ort"


def cur_temp():
    c = (S["state"].get("weather") or {}).get("current") or {}
    return c.get("temperature_2m")


async def add_stay(lat, lon, t_arr, src):
    stay = {"id": uuid.uuid4().hex[:8], "t": int(t_arr), "until": None, "lat": round(lat, 5), "lon": round(lon, 5),
            "place": await place_name(lat, lon), "temp": cur_temp(), "src": src, "note": ""}
    for s in TRIP["stays"]:
        if s.get("until") is None:
            s["until"] = int(t_arr)
    TRIP["stays"].append(stay)
    TRIP["stays"] = TRIP["stays"][-500:]
    trip_save()
    log().info("Reisetagebuch: Stellplatz %s", stay["place"])
    return stay


async def trip_tick():
    loc = S["location"]()
    if loc["src"] != "gps":
        return
    now = time.time()
    pos = (loc["lat"], loc["lon"])
    tr = TRIP["track"]
    if not tr or km_between((tr[-1][1], tr[-1][2]), pos) >= 0.05:
        if tr:
            d = km_between((tr[-1][1], tr[-1][2]), pos)
            if d < 50:            # Sprünge (GPS-Fehler) nicht mitzählen
                day = today().isoformat()
                TRIP["km"][day] = round(TRIP["km"].get(day, 0) + d, 2)
        tr.append([int(now), round(pos[0], 5), round(pos[1], 5)])
        if len(tr) > 30000:
            TRIP["track"] = tr[:10000:2] + tr[10000:]
    # Stellplatz: 60 Minuten im Umkreis von 250 m
    if _anchor["pos"] is None or km_between(_anchor["pos"], pos) > 0.25:
        _anchor["pos"], _anchor["t"] = pos, now
        op = next((s for s in TRIP["stays"] if s.get("until") is None), None)
        if op and km_between((op["lat"], op["lon"]), pos) > 1:
            op["until"] = int(now)
            trip_save()
    elif now - _anchor["t"] > 3600:
        op = next((s for s in TRIP["stays"] if s.get("until") is None), None)
        if not op or km_between((op["lat"], op["lon"]), pos) > 1:
            await add_stay(pos[0], pos[1], _anchor["t"], "gps")
            await S["broadcast"](trip_msg())


def trip_msg():
    tr = TRIP["track"]
    step = max(1, len(tr) // 3000)
    km_total = round(sum(TRIP["km"].values()), 1)
    return {"type": "trip", "track": [[p[1], p[2]] for p in tr[::step]], "stays": TRIP["stays"][-200:],
            "km_total": km_total, "km_days": sorted(TRIP["km"].items())[-30:], "gps": S["location"]()["src"] == "gps"}


async def trip_cmd(cmd):
    t = cmd.get("type")
    if t == "trip_here":
        loc = S["location"]()
        await add_stay(loc["lat"], loc["lon"], time.time(), loc["src"])
    elif t == "trip_del":
        TRIP["stays"] = [s for s in TRIP["stays"] if s.get("id") != cmd.get("id")]
        trip_save()
    elif t == "trip_note":
        for s in TRIP["stays"]:
            if s.get("id") == cmd.get("id"):
                s["note"] = str(cmd.get("note", ""))[:200]
        trip_save()
    elif t == "trip_clear_track":
        TRIP["track"] = []
        TRIP["km"] = {}
        trip_save()
    await S["broadcast"](trip_msg())


# ---------------------------------------------------------------------------
# Wartung
# ---------------------------------------------------------------------------
MAINT_DEFAULT = [
    ("Gasprüfung (G 607)", 24), ("Hauptuntersuchung (TÜV)", 24), ("Dichtigkeitsprüfung Aufbau", 12),
    ("Ölwechsel Iveco", 12), ("Bremsflüssigkeit", 24), ("Alde: Glykol prüfen", 24),
    ("Feuerlöscher prüfen", 24), ("Rauchmelder / CO-Melder testen", 12), ("Wasserfilter tauschen", 12),
]
MAINT = {"items": []}


def maint_load():
    d = jload("maintenance.json", None)
    if d and isinstance(d.get("items"), list):
        MAINT["items"] = d["items"]
    else:
        MAINT["items"] = [{"id": uuid.uuid4().hex[:8], "name": n, "months": m, "due": None, "last": None,
                           "notified": []} for n, m in MAINT_DEFAULT]


def maint_save():
    jsave("maintenance.json", MAINT)


def add_months(d, m):
    y, mo = divmod(d.month - 1 + m, 12)
    y += d.year
    mo += 1
    import calendar
    return dt.date(y, mo, min(d.day, calendar.monthrange(y, mo)[1]))


def maint_msg():
    items = []
    for x in MAINT["items"]:
        left = None
        if x.get("due"):
            left = (dt.date.fromisoformat(x["due"]) - today()).days
        items.append({**x, "left": left})
    items.sort(key=lambda x: (x["left"] is None, x["left"] if x["left"] is not None else 0))
    return {"type": "maint", "items": items}


async def maint_cmd(cmd):
    t, iid = cmd.get("type"), cmd.get("id")
    x = next((m for m in MAINT["items"] if m["id"] == iid), None)
    if t == "mt_done" and x:
        x["last"] = today().isoformat()
        x["due"] = add_months(today(), int(x.get("months") or 12)).isoformat()
        x["notified"] = []
    elif t == "mt_shift" and x:
        base = dt.date.fromisoformat(x["due"]) if x.get("due") else today()
        x["due"] = add_months(base, int(cmd.get("months", 1))).isoformat()
        x["notified"] = []
    elif t == "mt_clear" and x:
        x["due"] = None
        x["notified"] = []
    elif t == "mt_interval" and x:
        x["months"] = max(1, min(120, int(cmd.get("months", 12))))
    elif t == "mt_add" and str(cmd.get("name", "")).strip():
        MAINT["items"].append({"id": uuid.uuid4().hex[:8], "name": str(cmd["name"]).strip()[:50],
                               "months": max(1, min(120, int(cmd.get("months", 12)))), "due": None, "last": None,
                               "notified": []})
    elif t == "mt_del" and x:
        MAINT["items"].remove(x)
    maint_save()
    await S["broadcast"](maint_msg())


async def maint_tick():
    hour = dt.datetime.now().hour
    if not 9 <= hour < 20:
        return
    changed = False
    for x in MAINT["items"]:
        if not x.get("due"):
            continue
        left = (dt.date.fromisoformat(x["due"]) - today()).days
        hit = [t for t in (30, 7, 0) if left <= t]
        if not hit:
            continue
        th = min(hit)
        if th in x.setdefault("notified", []):
            continue
        x["notified"] = sorted(set(x["notified"]) | set(hit))
        changed = True
        if left > 0:
            text = f"{x['name']} ist in {left} Tagen fällig ({_de(x['due'])})."
        elif left == 0:
            text = f"{x['name']} ist heute fällig."
        else:
            text = f"{x['name']} ist seit {-left} Tagen überfällig (fällig war {_de(x['due'])})."
        await S["push"](f"🔧 Wartung: {x['name']}", text, "default")
    if changed:
        maint_save()
        await S["broadcast"](maint_msg())


def _de(iso):
    d = dt.date.fromisoformat(iso)
    return d.strftime("%d.%m.%Y")


# ---------------------------------------------------------------------------
# Zugriff vom Handy
# ---------------------------------------------------------------------------
def net_msg():
    ips = []
    try:
        out = subprocess.run(["hostname", "-I"], capture_output=True, text=True, timeout=5).stdout
        ips = [x for x in out.split() if "." in x and not x.startswith("127.") and not x.startswith("172.17.")]
    except Exception:
        pass
    ts = [x for x in ips if x.startswith("100.")]
    lan = [x for x in ips if not x.startswith("100.")]
    port = int(S["CFG"].get("web_port", 8080))
    return {"type": "net", "lan": lan, "tailscale": ts, "host": socket.gethostname(), "port": port}


# ---------------------------------------------------------------------------
# Start und Befehle
# ---------------------------------------------------------------------------
def snapshot_msgs():
    return [{"type": "warnings", "list": warn_list()}, {"type": "forecast", "forecast": FORECAST},
            check_msg(), maint_msg()]


async def handle(cmd, ws):
    """Befehle von der Oberfläche. True, wenn erledigt."""
    t = cmd.get("type", "")
    if t.startswith("chk_"):
        await check_cmd(cmd)
    elif t.startswith("trip_"):
        if t == "trip_get":
            await ws.send_str(json.dumps(trip_msg()))
        else:
            await trip_cmd(cmd)
    elif t.startswith("mt_"):
        await maint_cmd(cmd)
    elif t == "history":
        await ws.send_str(json.dumps(hist_get(cmd.get("range", "24h"))))
    elif t == "net_info":
        await ws.send_str(json.dumps(net_msg()))
    elif t == "warn_set":
        key, v = cmd.get("key"), cmd.get("value")
        w = S["CFG"].setdefault("warnings", {})
        if key == "toggle" and v in WARN_IDS:
            off = set(w.get("off", []))
            off.symmetric_difference_update({v})
            w["off"] = sorted(off)
        elif key in ("enabled", "shore_lost", "push"):
            w[key] = bool(v)
        elif key in WARN_DEFAULTS and num(v) is not None:
            w[key] = num(v)
        S["save_config"]()
        await S["broadcast"]({"type": "warn_cfg", "cfg": wcfg()})
        await warnings_tick()
    elif t == "warn_cfg":
        await ws.send_str(json.dumps({"type": "warn_cfg", "cfg": wcfg(), "names": WARN_NAMES}))
    elif t == "energy_set":
        e = S["CFG"].setdefault("energy", {})
        for k in ("battery_wh", "pv_wp", "min_soc"):
            if k in cmd and num(cmd[k]) is not None:
                e[k] = num(cmd[k])
        S["save_config"]()
        FORECAST.clear()
        FORECAST.update(forecast_calc())
        await S["broadcast"]({"type": "forecast", "forecast": FORECAST})
    else:
        return False
    return True


async def _guard(name, coro_fn, every):
    while True:
        try:
            await coro_fn()
        except Exception as e:
            log().warning("%s: %s", name, e)
        await asyncio.sleep(every)


_tick = {"hist": 0, "move": 0}


async def _hist_loop():
    hist_sample()
    _tick["hist"] += 1
    if _tick["hist"] % 10 == 0:          # alle 10 Minuten auf die SD-Karte
        hist_save()


async def _forecast_loop():
    FORECAST.clear()
    FORECAST.update(forecast_calc())
    await S["broadcast"]({"type": "forecast", "forecast": FORECAST})


async def _move_loop():
    await drive_tick()
    await trip_tick()
    _tick["move"] += 1
    if _tick["move"] % 30 == 0 and TRIP["track"]:      # alle 5 Minuten
        trip_save()


def start():
    hist_load()
    check_load()
    trip_load()
    maint_load()
    for name, fn, every in (("Warnungen", warnings_tick, 30), ("Verlauf", _hist_loop, 60),
                            ("Prognose", _forecast_loop, 120), ("Fahrt", _move_loop, 10),
                            ("Wartung", maint_tick, 3600)):
        asyncio.get_running_loop().create_task(_guard(name, fn, every))


def shutdown():
    hist_save()
    trip_save()
