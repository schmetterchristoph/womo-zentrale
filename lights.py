"""Licht über Shelly (lokal im WLAN, ohne Cloud).

Unterstützt:
- Shelly Gen2/Gen3/Gen4 (Plus/Pro): Light (Dimmer, RGBW PM im Licht-Modus mit 4 Kanälen), Switch (Plus 1, Uni ...),
  RGBW (RGBW PM im Farb-Modus)
- Shelly Gen1: RGBW2 (white/color), Dimmer, Shelly 1 (relay)

Lampen ohne zugeordneten Shelly werden nur auf dem Bildschirm geschaltet ("nicht verbunden"),
damit man die Oberfläche schon vorher ausprobieren kann.
"""
import asyncio
import ipaddress
import json
import subprocess
import time
import uuid

import aiohttp

S = {}          # von server.py: CFG, broadcast, save_config, log

ZONES = {       # Bereiche im Grundriss (Draufsicht, Fahrerhaus links)
    "alkoven": "Alkoven", "sitz": "Sitzgruppe", "kueche": "Küche", "bad": "Bad", "heck": "Heckbett",
    "eingang": "Eingang", "decke": "Wohnraum", "ambiente": "Ambiente", "aussen": "Außen / Vorzelt", "garage": "Heckgarage",
}
DEFAULT_LIGHTS = [
    ("Alkoven", "alkoven", True), ("Sitzgruppe", "sitz", True), ("Deckenlicht", "decke", True),
    ("Küche", "kueche", True), ("Bad", "bad", True), ("Heckbett", "heck", True),
    ("Ambiente", "ambiente", True), ("Einstieg", "eingang", True), ("Vorzelt", "aussen", False),
]
SCENES = {
    "aus":     {"name": "Alles aus", "set": {"*": 0}},
    "hell":    {"name": "Hell", "set": {"*": 100, "aussen": None, "garage": None}},
    "abend":   {"name": "Abend", "set": {"*": 0, "sitz": 45, "ambiente": 35, "kueche": 30, "decke": 0}},
    "nacht":   {"name": "Nacht", "set": {"*": 0, "bad": 8, "eingang": 5}},
    "ankommen": {"name": "Ankommen", "set": {"eingang": 100, "decke": 80, "aussen": 100, "ambiente": 50}},
}

LIGHTS = {}          # id -> Laufzeit-Zustand {"on","bri","ok","err","t"}
DEV = {}             # host -> {"gen": int, "t": letzte Abfrage}
SCAN = {"busy": False, "found": [], "t": 0}


def setup(**kw):
    S.update(kw)


def log():
    return S["log"]


def cfg():
    c = S["CFG"].setdefault("lights", {})
    if "list" not in c:
        c["list"] = [{"id": uuid.uuid4().hex[:6], "name": n, "zone": z, "dim": d, "host": "", "ch": 0, "kind": "auto"}
                     for n, z, d in DEFAULT_LIGHTS]
        S["save_config"]()
    return c


def lights_msg():
    out = []
    for l in cfg()["list"]:
        st = LIGHTS.get(l["id"], {})
        out.append({**l, "on": bool(st.get("on")), "bri": st.get("bri", 100 if l.get("dim") else None),
                    "ok": bool(st.get("ok")), "virtual": not l.get("host"), "err": st.get("err", ""),
                    "power": st.get("power")})
    return {"type": "lights", "lights": out, "zones": ZONES,
            "scenes": [{"id": k, "name": v["name"]} for k, v in SCENES.items()]}


# ---------------------------------------------------------------------------
# Shelly-Kommunikation
# ---------------------------------------------------------------------------
_session = {"s": None}


def session():
    if _session["s"] is None or _session["s"].closed:
        _session["s"] = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=4))
    return _session["s"]


async def http_json(url, timeout=4):
    async with session().get(url, timeout=aiohttp.ClientTimeout(total=timeout)) as r:
        if r.status != 200:
            raise IOError(f"HTTP {r.status}")
        return await r.json(content_type=None)


async def dev_gen(host):
    d = DEV.get(host)
    if d and time.time() - d["t"] < 600:
        return d["gen"]
    info = await http_json(f"http://{host}/shelly")
    gen = int(info.get("gen") or 1)
    DEV[host] = {"gen": gen, "t": time.time(), "model": info.get("model") or info.get("type") or ""}
    return gen


def kind_of(l, status_keys=()):
    k = l.get("kind") or "auto"
    if k != "auto":
        return k
    ch = int(l.get("ch") or 0)
    for want in ("light", "rgbw", "rgb", "switch"):
        if f"{want}:{ch}" in status_keys:
            return want
    return "light" if l.get("dim") else "switch"


async def read_light(l):
    host, ch = l["host"], int(l.get("ch") or 0)
    gen = await dev_gen(host)
    if gen >= 2:
        st = await http_json(f"http://{host}/rpc/Shelly.GetStatus")
        k = kind_of(l, st.keys())
        x = st.get(f"{k}:{ch}") or {}
        return {"on": bool(x.get("output")), "bri": x.get("brightness"), "power": x.get("apower"), "kind": k}
    st = await http_json(f"http://{host}/status")
    if st.get("lights"):
        x = st["lights"][ch] if ch < len(st["lights"]) else {}
        return {"on": bool(x.get("ison")), "bri": x.get("brightness", x.get("gain")), "power": None, "kind": "light"}
    if st.get("relays"):
        x = st["relays"][ch] if ch < len(st["relays"]) else {}
        return {"on": bool(x.get("ison")), "bri": None, "power": None, "kind": "switch"}
    raise IOError("unbekannter Shelly")


async def write_light(l, on, bri=None):
    host, ch = l["host"], int(l.get("ch") or 0)
    gen = await dev_gen(host)
    if gen >= 2:
        k = LIGHTS.get(l["id"], {}).get("kind") or kind_of(l)
        meth = {"light": "Light", "rgbw": "RGBW", "rgb": "RGB", "switch": "Switch"}.get(k, "Light")
        q = f"id={ch}&on={'true' if on else 'false'}"
        if bri is not None and meth != "Switch":
            q += f"&brightness={int(bri)}"
        await http_json(f"http://{host}/rpc/{meth}.Set?{q}")
    else:
        st = await http_json(f"http://{host}/status")
        path = "light" if st.get("lights") else "relay"
        q = f"turn={'on' if on else 'off'}"
        if bri is not None and path == "light":
            q += f"&brightness={int(bri)}"
        await http_json(f"http://{host}/{path}/{ch}?{q}")


async def set_light(lid, on=None, bri=None, quiet=False):
    l = next((x for x in cfg()["list"] if x["id"] == lid), None)
    if not l:
        return False
    st = LIGHTS.setdefault(lid, {"on": False, "bri": 100})
    if on is None:
        on = not st.get("on")
    if bri is not None:
        bri = max(1, min(100, int(bri)))
        if not l.get("dim"):
            bri = None
    st["on"] = on
    if bri is not None:
        st["bri"] = bri
    ok = True
    if l.get("host"):
        try:
            await write_light(l, on, bri)
            st["ok"], st["err"] = True, ""
        except Exception as e:
            st["ok"], st["err"], ok = False, f"Shelly nicht erreichbar ({type(e).__name__})", False
            log().warning("Licht %s: %s", l["name"], e)
    if not quiet:
        await S["broadcast"](lights_msg())
    return ok


def find_lights(words):
    """Lampen passend zu einem Namen/Bereich (für Sprache und Szenen)."""
    w = words.lower()
    res = []
    for l in cfg()["list"]:
        n = l["name"].lower()
        z = ZONES.get(l.get("zone"), "").lower()
        if n in w or (z and z.split(" ")[0] in w) or any(p in w for p in n.split() if len(p) > 3):
            res.append(l)
    return res


async def scene(sid):
    sc = SCENES.get(sid)
    if not sc:
        return
    tasks = []
    for l in cfg()["list"]:
        z = l.get("zone")
        v = sc["set"].get(z, sc["set"].get("*", "skip")) if z in sc["set"] or "*" in sc["set"] else "skip"
        if v == "skip" or v is None:
            continue
        tasks.append(set_light(l["id"], on=v > 0, bri=v if v > 0 else None, quiet=True))
    await asyncio.gather(*tasks)
    await S["broadcast"](lights_msg())


async def all_lights(on):
    await asyncio.gather(*[set_light(l["id"], on=on, quiet=True) for l in cfg()["list"] if l.get("zone") != "aussen" or not on])
    await S["broadcast"](lights_msg())


# ---------------------------------------------------------------------------
# Shellys im WLAN suchen
# ---------------------------------------------------------------------------
def local_nets():
    nets = []
    try:
        out = subprocess.run(["ip", "-4", "-o", "addr", "show"], capture_output=True, text=True, timeout=5).stdout
        for line in out.splitlines():
            p = line.split()
            if "inet" in p:
                cidr = p[p.index("inet") + 1]
                ifc = ipaddress.ip_interface(cidr)
                if ifc.ip.is_loopback or str(ifc.ip).startswith("172.17."):
                    continue
                net = ifc.network if ifc.network.prefixlen >= 22 else ipaddress.ip_network(f"{ifc.ip}/24", strict=False)
                nets.append(net)
    except Exception:
        pass
    return nets


async def probe(ip, sem, found):
    async with sem:
        try:
            info = await http_json(f"http://{ip}/shelly", timeout=1.5)
        except Exception:
            return
    if not isinstance(info, dict) or not (info.get("mac") or info.get("id")):
        return
    gen = int(info.get("gen") or 1)
    chans = []
    try:
        if gen >= 2:
            st = await http_json(f"http://{ip}/rpc/Shelly.GetStatus", timeout=3)
            for k in st:
                kind, _, n = k.partition(":")
                if kind in ("light", "switch", "rgbw", "rgb") and n.isdigit():
                    chans.append({"ch": int(n), "kind": kind, "on": bool(st[k].get("output"))})
        else:
            st = await http_json(f"http://{ip}/status", timeout=3)
            for i, x in enumerate(st.get("lights") or []):
                chans.append({"ch": i, "kind": "light", "on": bool(x.get("ison"))})
            for i, x in enumerate(st.get("relays") or []):
                chans.append({"ch": i, "kind": "switch", "on": bool(x.get("ison"))})
    except Exception:
        pass
    found.append({"host": str(ip), "gen": gen, "model": info.get("model") or info.get("type") or "Shelly",
                  "name": info.get("name") or info.get("id") or "", "chans": sorted(chans, key=lambda c: (c["kind"], c["ch"]))})


async def scan():
    if SCAN["busy"]:
        return
    SCAN.update(busy=True, found=[])
    await S["broadcast"]({"type": "shelly_scan", "busy": True, "found": []})
    found = []
    sem = asyncio.Semaphore(48)
    hosts = []
    for net in local_nets():
        hosts += [h for h in net.hosts()][:1022]
    try:
        await asyncio.gather(*[probe(h, sem, found) for h in hosts])
    except Exception as e:
        log().warning("Shelly-Suche: %s", e)
    found.sort(key=lambda d: tuple(int(x) for x in d["host"].split(".")))
    SCAN.update(busy=False, found=found, t=time.time())
    log().info("Shelly-Suche: %d Geräte in %d Adressen", len(found), len(hosts))
    await S["broadcast"]({"type": "shelly_scan", "busy": False, "found": found, "searched": True})


# ---------------------------------------------------------------------------
# Befehle und Abfrage
# ---------------------------------------------------------------------------
async def handle(cmd, ws):
    t = cmd.get("type", "")
    if not t.startswith("light"):
        if t == "shelly_scan":
            asyncio.get_running_loop().create_task(scan())
            return True
        return False
    lst = cfg()["list"]
    if t == "light_set":
        await set_light(cmd.get("id"), on=cmd.get("on"), bri=cmd.get("bri"))
    elif t == "light_scene":
        await scene(cmd.get("scene"))
    elif t == "light_all":
        await all_lights(bool(cmd.get("on")))
    elif t == "light_get":
        await ws.send_str(json.dumps(lights_msg()))
        if SCAN["found"]:
            await ws.send_str(json.dumps({"type": "shelly_scan", "busy": SCAN["busy"], "found": SCAN["found"], "searched": True}))
        return True
    elif t == "light_edit":
        l = next((x for x in lst if x["id"] == cmd.get("id")), None)
        if l:
            for k in ("name", "zone", "host", "ch", "dim", "kind"):
                if k in cmd:
                    l[k] = cmd[k] if k != "name" else str(cmd[k]).strip()[:24] or l["name"]
            if "host" in cmd:
                LIGHTS.pop(l["id"], None)
            S["save_config"]()
    elif t == "light_add":
        lst.append({"id": uuid.uuid4().hex[:6], "name": str(cmd.get("name") or "Neues Licht")[:24],
                    "zone": cmd.get("zone") if cmd.get("zone") in ZONES else "decke", "dim": True, "host": "", "ch": 0,
                    "kind": "auto"})
        S["save_config"]()
    elif t == "light_del":
        cfg()["list"] = [x for x in lst if x["id"] != cmd.get("id")]
        LIGHTS.pop(cmd.get("id"), None)
        S["save_config"]()
    else:
        return False
    await S["broadcast"](lights_msg())
    return True


async def poll_loop():
    await asyncio.sleep(3)
    last = None
    while True:
        changed = False
        for l in list(cfg()["list"]):
            if not l.get("host"):
                continue
            st = LIGHTS.setdefault(l["id"], {"on": False, "bri": 100})
            try:
                r = await read_light(l)
                new = {"on": r["on"], "bri": r["bri"] if r["bri"] is not None else st.get("bri"), "ok": True,
                       "err": "", "kind": r["kind"], "power": r["power"]}
            except Exception as e:
                new = {**st, "ok": False, "err": f"Shelly nicht erreichbar ({type(e).__name__})"}
            if any(st.get(k) != new.get(k) for k in ("on", "bri", "ok", "power")):
                changed = True
            st.update(new)
        snap = json.dumps(lights_msg(), sort_keys=True)
        if changed or snap != last:
            last = snap
            await S["broadcast"](lights_msg())
        await asyncio.sleep(3)


def start():
    cfg()
    asyncio.get_running_loop().create_task(poll_loop())
