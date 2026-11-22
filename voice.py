"""Sprachsteuerung – läuft komplett auf dem Pi (ohne Cloud).

Erkennung: Vosk (deutsches Modell, offline). Antwort: Piper (natürliche deutsche Stimme), sonst espeak-ng.
Aufnahme/Wiedergabe über ALSA (arecord/aplay), Mikrofon wird automatisch gefunden (USB bevorzugt).

Weckwort (Standard): „Zentrale“ oder „Computer“, z. B. „Zentrale, Licht aus“.
Auf dem Bildschirm gibt es zusätzlich eine Mikrofon-Taste (ohne Weckwort sprechen).

Einrichtung ohne Terminal: in der Zentrale unter Extras → Sprache „Einrichten“ tippen – lädt Programme und Modelle.
"""
import asyncio
import datetime as dt
import json
import re
import shutil
import subprocess
import sys
import threading
import time
import zipfile

import aiohttp

S = {}            # von server.py: CFG, BASE, broadcast, save_config, log, values, state, lights, extras, gree_set, guard_set

VOSK_URL = "https://alphacephei.com/vosk/models/vosk-model-small-de-0.15.zip"
VOSK_DIR = "vosk-model-small-de-0.15"
PIPER_BASE = "https://huggingface.co/rhasspy/piper-voices/resolve/main/de/de_DE/thorsten/medium/"
PIPER_VOICE = "de_DE-thorsten-medium.onnx"

V = {"state": "aus", "status": "", "listen_until": 0, "speaking": False, "heard": "", "reply": "", "t": 0,
     "setup_busy": False, "mic": None, "thread": None, "stop": False, "proc": None, "loop": None, "partial": ""}


def setup(**kw):
    S.update(kw)


def log():
    return S["log"]


def cfg():
    c = S["CFG"].setdefault("voice", {})
    c.setdefault("enabled", False)
    c.setdefault("wake", ["zentrale", "computer"])
    c.setdefault("mic", "auto")
    c.setdefault("speaker", "auto")
    c.setdefault("speak", True)
    return c


def models_dir():
    d = S["BASE"] / "models"
    d.mkdir(exist_ok=True)
    return d


# ---------------------------------------------------------------------------
# Was ist vorhanden?
# ---------------------------------------------------------------------------
def have_vosk():
    try:
        import vosk  # noqa: F401
        return True
    except Exception:
        return False


def have_piper():
    try:
        import piper  # noqa: F401
        return (models_dir() / PIPER_VOICE).exists()
    except Exception:
        return False


def alsa_cards(kind):
    """kind = 'arecord' oder 'aplay' -> [(karte, name)]"""
    try:
        out = subprocess.run([kind, "-l"], capture_output=True, text=True, timeout=5).stdout
    except Exception:
        return []
    res = []
    for m in re.finditer(r"^(?:card|Karte) (\d+): (\S+) \[([^\]]*)\]", out, re.M):
        res.append((m.group(2), m.group(3)))
    return res


def find_mic():
    if cfg()["mic"] != "auto":
        return cfg()["mic"]
    cards = alsa_cards("arecord")
    if not cards:
        return None
    pref = sorted(cards, key=lambda c: (not re.search(r"respeaker|xvf|usb|jabra|mic|array|anker", c[0] + c[1], re.I)))
    return f"plughw:CARD={pref[0][0]},DEV=0"


def find_speaker():
    if cfg()["speaker"] != "auto":
        return cfg()["speaker"]
    mic = V.get("mic") or ""
    card = mic.split("CARD=")[1].split(",")[0] if "CARD=" in mic else None
    play = alsa_cards("aplay")
    if card and any(c[0] == card for c in play):
        return f"plughw:CARD={card},DEV=0"
    return "default"


def status():
    c = cfg()
    return {"type": "voice", "enabled": c["enabled"], "state": V["state"], "status": V["status"],
            "vosk": have_vosk() and (models_dir() / VOSK_DIR).exists(), "piper": have_piper(),
            "espeak": bool(shutil.which("espeak-ng")), "mic": V["mic"] or find_mic(), "wake": c["wake"],
            "heard": V["heard"], "reply": V["reply"], "t": V["t"], "busy": V["setup_busy"], "speak": c["speak"]}


async def push_status():
    await S["broadcast"](status())


# ---------------------------------------------------------------------------
# Einrichten: Programme (pip, ohne sudo) und Modelle laden
# ---------------------------------------------------------------------------
async def setup_voice():
    if V["setup_busy"]:
        return
    V["setup_busy"] = True
    try:
        if not have_vosk() or not _has_module("piper"):
            V["status"] = "Lade Programme (Vosk, Piper) – das dauert ein paar Minuten ..."
            await push_status()
            p = await asyncio.create_subprocess_exec(
                sys.executable, "-m", "pip", "install", "--user", "--break-system-packages", "--quiet", "vosk", "piper-tts",
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT)
            out, _ = await p.communicate()
            if p.returncode != 0:
                log().warning("pip: %s", out.decode(errors="ignore")[-500:])
                V["status"] = "Programme konnten nicht installiert werden (Internet?)."
            import importlib
            import site
            importlib.invalidate_caches()
            try:
                sys.path.append(site.getusersitepackages())
            except Exception:
                pass
        md = models_dir()
        async with aiohttp.ClientSession() as s:
            if not (md / VOSK_DIR).exists():
                V["status"] = "Lade Spracherkennung (ca. 45 MB) ..."
                await push_status()
                zp = md / "vosk.zip"
                await _download(s, VOSK_URL, zp)
                with zipfile.ZipFile(zp) as z:
                    z.extractall(md)
                zp.unlink(missing_ok=True)
            if not (md / PIPER_VOICE).exists():
                V["status"] = "Lade Stimme (ca. 60 MB) ..."
                await push_status()
                await _download(s, PIPER_BASE + PIPER_VOICE + ".json", md / (PIPER_VOICE + ".json"))
                await _download(s, PIPER_BASE + PIPER_VOICE, md / PIPER_VOICE)
        cfg()["enabled"] = True
        S["save_config"]()
        V["status"] = "Fertig eingerichtet."
        await start_listening()
    except Exception as e:
        log().warning("Spracheinrichtung: %s", e)
        V["status"] = f"Einrichtung fehlgeschlagen: {e}"[:120]
    finally:
        V["setup_busy"] = False
        await push_status()


def _has_module(name):
    import importlib.util
    return importlib.util.find_spec(name) is not None


async def _download(s, url, dest):
    tmp = dest.with_suffix(dest.suffix + ".part")
    async with s.get(url, timeout=aiohttp.ClientTimeout(total=900)) as r:
        if r.status != 200:
            raise IOError(f"Download {url.split('/')[-1]}: HTTP {r.status}")
        with open(tmp, "wb") as f:
            async for chunk in r.content.iter_chunked(1 << 16):
                f.write(chunk)
    tmp.replace(dest)


# ---------------------------------------------------------------------------
# Zuhören
# ---------------------------------------------------------------------------
async def start_listening():
    stop_listening()
    if not cfg()["enabled"]:
        V["state"] = "aus"
        return
    if not (have_vosk() and (models_dir() / VOSK_DIR).exists()):
        V["state"], V["status"] = "aus", "Noch nicht eingerichtet."
        return
    mic = find_mic()
    if not mic or not shutil.which("arecord"):
        V["state"], V["status"] = "aus", "Kein Mikrofon gefunden – bitte USB-Mikrofon anstecken."
        return
    V["mic"], V["stop"], V["loop"] = mic, False, asyncio.get_running_loop()
    V["thread"] = threading.Thread(target=_listen_thread, daemon=True)
    V["thread"].start()
    V["state"], V["status"] = "bereit", f"Hört zu über {mic}"
    log().info("Sprachsteuerung aktiv (%s)", mic)


def stop_listening():
    V["stop"] = True
    p = V.get("proc")
    if p:
        try:
            p.terminate()
        except Exception:
            pass
    V["proc"] = None


def _listen_thread():
    from vosk import KaldiRecognizer, Model, SetLogLevel
    SetLogLevel(-1)
    model = Model(str(models_dir() / VOSK_DIR))
    while not V["stop"]:
        rec = KaldiRecognizer(model, 16000)
        try:
            p = subprocess.Popen(["arecord", "-q", "-D", V["mic"], "-f", "S16_LE", "-r", "16000", "-c", "1", "-t", "raw"],
                                 stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
        except Exception as e:
            _post(_set_status, f"Mikrofon-Fehler: {e}")
            return
        V["proc"] = p
        while not V["stop"]:
            data = p.stdout.read(4000)
            if not data:
                break
            if V["speaking"]:
                continue                      # eigene Antwort nicht hören
            if rec.AcceptWaveform(data):
                text = json.loads(rec.Result()).get("text", "")
                if text:
                    _post(on_text, text)
            else:
                part = json.loads(rec.PartialResult()).get("partial", "")
                if part and part != V["partial"]:
                    V["partial"] = part
                    if _wake_in(part) and V["state"] != "hört":
                        _post(_set_state, "hört")
        try:
            p.terminate()
        except Exception:
            pass
        if not V["stop"]:
            time.sleep(2)                     # Mikrofon kurz weg (z. B. umgesteckt) -> neu versuchen


def _post(fn, *a):
    loop = V.get("loop")
    if loop:
        asyncio.run_coroutine_threadsafe(fn(*a), loop)


async def _set_status(t):
    V["status"] = t
    await push_status()


async def _set_state(st):
    V["state"] = st
    await S["broadcast"]({"type": "voice_state", "state": st})


def _wake_in(text):
    return any(w in text.lower().split() or w in text.lower() for w in cfg()["wake"])


async def listen_now():
    """Mikrofon-Taste auf dem Bildschirm: 8 Sekunden zuhören ohne Weckwort."""
    V["listen_until"] = time.time() + 8
    await _set_state("hört")
    await play_beep()


async def on_text(text):
    t = text.lower().strip()
    V["partial"] = ""
    wake = next((w for w in cfg()["wake"] if w in t), None)
    if wake:
        cmd = t.split(wake, 1)[1].strip(" ,.")
        if not cmd:
            V["listen_until"] = time.time() + 7
            await _set_state("hört")
            await play_beep()
            return
    elif time.time() < V["listen_until"]:
        cmd = t
    else:
        return
    V["listen_until"] = 0
    await _set_state("denkt")
    try:
        reply = await handle_command(cmd)
    except Exception as e:
        log().warning("Sprachbefehl '%s': %s", cmd, e)
        reply = "Da ist etwas schiefgegangen."
    V.update(heard=cmd, reply=reply, t=time.time())
    log().info("Sprache: '%s' -> %s", cmd, reply)
    await S["broadcast"]({"type": "voice_event", "heard": cmd, "reply": reply})
    await speak(reply)
    await _set_state("bereit" if V.get("thread") and not V["stop"] else "aus")


# ---------------------------------------------------------------------------
# Sprechen
# ---------------------------------------------------------------------------
async def play_beep():
    spk = find_speaker()
    wav = models_dir() / "beep.wav"
    if not wav.exists():
        import math
        import struct
        import wave
        with wave.open(str(wav), "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(22050)
            frames = b"".join(struct.pack("<h", int(9000 * math.sin(2 * math.pi * (880 if i < 2400 else 1320) * i / 22050)
                                                    * min(1, (4800 - i) / 600, i / 300))) for i in range(4800))
            w.writeframes(frames)
    if shutil.which("aplay"):
        p = await asyncio.create_subprocess_exec("aplay", "-q", "-D", spk, str(wav),
                                                 stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL)
        await p.wait()


async def speak(text):
    if not text or not cfg().get("speak", True) or not shutil.which("aplay"):
        return
    spk = find_speaker()
    out = models_dir() / "say.wav"
    V["speaking"] = True
    try:
        ok = False
        if have_piper():
            p = await asyncio.create_subprocess_exec(sys.executable, "-m", "piper", "--model", str(models_dir() / PIPER_VOICE),
                                                     "--output_file", str(out), stdin=asyncio.subprocess.PIPE,
                                                     stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL)
            await p.communicate(text.encode())
            ok = p.returncode == 0 and out.exists()
        if not ok and shutil.which("espeak-ng"):
            p = await asyncio.create_subprocess_exec("espeak-ng", "-v", "de", "-s", "150", "-w", str(out), text,
                                                     stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL)
            await p.wait()
            ok = p.returncode == 0
        if ok:
            p = await asyncio.create_subprocess_exec("aplay", "-q", "-D", spk, str(out),
                                                     stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL)
            await p.wait()
    finally:
        await asyncio.sleep(.3)
        V["speaking"] = False


# ---------------------------------------------------------------------------
# Befehle verstehen
# ---------------------------------------------------------------------------
NUM = {"null": 0, "ein": 1, "eins": 1, "eine": 1, "einen": 1, "zwei": 2, "drei": 3, "vier": 4, "fünf": 5, "sechs": 6,
       "sieben": 7, "acht": 8, "neun": 9, "zehn": 10, "elf": 11, "zwölf": 12, "dreizehn": 13, "vierzehn": 14,
       "fünfzehn": 15, "sechzehn": 16, "siebzehn": 17, "achtzehn": 18, "neunzehn": 19, "zwanzig": 20, "dreißig": 30,
       "vierzig": 40, "fünfzig": 50, "sechzig": 60, "siebzig": 70, "achtzig": 80, "neunzig": 90, "hundert": 100,
       "einhundert": 100, "halb": 50}


def word_num(w):
    w = w.replace("ß", "ß")
    if w.isdigit():
        return int(w)
    if w in NUM:
        return NUM[w]
    m = re.fullmatch(r"(\w+?)und(\w+)", w)
    if m and m.group(1) in NUM and m.group(2) in NUM and NUM[m.group(2)] % 10 == 0:
        return NUM[m.group(1)] + NUM[m.group(2)]
    return None


def first_num(text):
    for w in re.findall(r"[\wäöüß]+", text):
        n = word_num(w)
        if n is not None:
            return n
    return None


def has(text, *words):
    return any(re.search(rf"\b{w}", text) for w in words)


def onoff(text):
    if has(text, "aus", "ausschalten", "ausmachen", "abschalten", "stopp", "stop", "beenden"):
        return False
    if has(text, "an\\b", "ein\\b", "einschalten", "anmachen", "anschalten", "starten", "start"):
        return True
    return None


def fmt(x, d=0):
    return f"{x:.{d}f}".replace(".", ",")


async def ui(**kw):
    await S["broadcast"]({"type": "voice_ui", **kw})


async def handle_command(t):
    L, X = S["lights"], S["extras"]
    t = t.lower()

    if has(t, "nachtmodus"):
        on = onoff(t) is not False
        await ui(do="night", on=on)
        return "Nachtmodus " + ("an." if on else "aus.")

    # --- Licht -------------------------------------------------------------
    scene_words = {"abend": "abend", "nacht": "nacht", "ankommen": "ankommen", "willkommen": "ankommen"}
    for w, sid in scene_words.items():
        if w in t and has(t, "licht", "szene", "modus", "stimmung", "beleuchtung") or t.strip() in (w, w + "licht"):
            await L.scene(sid)
            return f"Szene {L.SCENES[sid]['name']}."
    targets = L.find_lights(t)
    if has(t, "licht", "lichter", "lampe", "lampen", "beleuchtung", "hell", "dunkel", "dimm") or (targets and (onoff(t) is not None or first_num(t) is not None or has(t, "heller", "dunkler"))):
        on = onoff(t)
        n = first_num(t)
        if has(t, "alle", "alles", "überall", "komplett") or (not targets and on is False):
            if on is False:
                await L.scene("aus")
                return "Alle Lichter sind aus."
            if on is True or has(t, "hell"):
                await L.scene("hell")
                return "Alle Lichter an."
        if not targets:
            lit = [l for l in L.cfg()["list"] if L.LIGHTS.get(l["id"], {}).get("on") and l.get("zone") != "aussen"]
            targets = lit if lit and (has(t, "heller", "dunkler") or n is not None) else \
                [l for l in L.cfg()["list"] if l.get("zone") not in ("aussen",)]
            if on is None and n is None and not has(t, "heller", "dunkler"):
                return "Welches Licht meinst du?"
        names = ", ".join(l["name"] for l in targets[:3])
        if has(t, "heller", "dunkler"):
            d = 25 if has(t, "heller") else -25
            for l in targets:
                cur = L.LIGHTS.get(l["id"], {}).get("bri") or 60
                await L.set_light(l["id"], on=True, bri=max(5, min(100, cur + d)), quiet=True)
            await S["broadcast"](L.lights_msg())
            return f"{names} {'heller' if d > 0 else 'dunkler'}."
        if n is not None and has(t, "prozent", "auf", "dimm"):
            for l in targets:
                await L.set_light(l["id"], on=n > 0, bri=n if n > 0 else None, quiet=True)
            await S["broadcast"](L.lights_msg())
            return f"{names} auf {n} Prozent."
        if on is None:
            on = not any(L.LIGHTS.get(l["id"], {}).get("on") for l in targets)
        for l in targets:
            await L.set_light(l["id"], on=on, quiet=True)
        await S["broadcast"](L.lights_msg())
        return f"{names} {'an' if on else 'aus'}."

    # --- Klima -------------------------------------------------------------
    if has(t, "klima", "kühl", "heiz", "lüft"):
        cmd = {}
        on = onoff(t)
        n = first_num(t)
        if on is not None:
            cmd["power"] = on
        if n is not None and 16 <= n <= 30:
            cmd.update(power=True, target=n)
        if has(t, "kühl"):
            cmd.update(power=True, mode="cool")
        elif has(t, "heiz"):
            cmd.update(power=True, mode="heat")
        elif has(t, "lüft"):
            cmd.update(power=True, mode="fan")
        ac = S["state"].get("ac") or {}
        if not cmd:
            if ac.get("ok"):
                return f"Die Klimaanlage ist {'an' if ac.get('power') else 'aus'}, eingestellt auf {ac.get('target')} Grad, im Raum sind es {ac.get('current')} Grad."
            return "Die Klimaanlage ist nicht verbunden."
        if not ac.get("ok"):
            return "Die Klimaanlage ist nicht verbunden."
        await S["gree_set"](cmd)
        if cmd.get("power") is False:
            return "Klimaanlage aus."
        return "Klimaanlage an" + (f", {cmd['target']} Grad." if "target" in cmd else ".")

    # --- Fragen ------------------------------------------------------------
    if has(t, "akku", "batterie", "strom", "ladung"):
        soc = X.soc_now()
        if soc is None:
            return "Ich habe gerade keine Akkudaten."
        f = X.FORECAST or {}
        extra = f" {f['text'][0].upper() + f['text'][1:]}." if f.get("text") and not f.get("shore") else (" Landstrom ist angeschlossen." if X.shore_on() else "")
        return f"Der Akku hat {fmt(soc)} Prozent.{extra}"
    if has(t, "grauwasser", "abwasser"):
        _, lv = X.tank_level([2])
        return "Ich habe keinen Grauwassertank gefunden." if lv is None else f"Der Grauwassertank ist zu {fmt(lv)} Prozent voll."
    if has(t, "toilette", "klo", "kassette", "schwarzwasser"):
        _, lv = X.tank_level([5])
        return "Ich habe keinen Toilettentank gefunden." if lv is None else f"Die Toilette ist zu {fmt(lv)} Prozent voll."
    if has(t, "wasser"):
        _, lv = X.tank_level([1])
        return "Ich habe keinen Frischwassertank gefunden." if lv is None else f"Frischwasser: noch {fmt(lv)} Prozent."
    if has(t, "gas", "flasche"):
        g = (S.get("sensors") and S["sensors"].gas_summary()) or None
        return g or "Ich habe keinen Gassensor."
    if has(t, "reifen", "luftdruck"):
        g = (S.get("sensors") and S["sensors"].tpms_summary()) or None
        return g or "Ich habe keine Reifendrucksensoren."
    if has(t, "wetter", "regnet", "regen", "morgen"):
        return weather_text(t)
    if has(t, "temperatur", "warm", "kalt", "grad"):
        n, ti = X.temp_by_name(r"innen|wohn|raum")
        no, to = X.temp_by_name(r"außen|aussen|outside|outdoor")
        if to is None:
            to = ((S["state"].get("weather") or {}).get("current") or {}).get("temperature_2m")
        parts = []
        if ti is not None:
            parts.append(f"innen {fmt(ti)} Grad")
        if to is not None:
            parts.append(f"außen {fmt(to)} Grad")
        return ("Es sind " + " und ".join(parts) + ".") if parts else "Ich habe keine Temperaturwerte."
    if has(t, "uhr", "spät", "uhrzeit"):
        n = dt.datetime.now()
        return f"Es ist {n.hour} Uhr {n.minute}."
    if has(t, "alarm", "bewegungsalarm", "überwachung"):
        on = onoff(t)
        if on is None and has(t, "scharf"):
            on = True
        if on is None:
            return "Soll ich den Bewegungsalarm an- oder ausschalten?"
        await S["guard_set"](on)
        return "Bewegungsalarm ist scharf." if on else "Bewegungsalarm ist aus."

    # --- Bildschirm --------------------------------------------------------
    if has(t, "radio", "musik"):
        on = onoff(t)
        if has(t, "lauter"):
            await ui(do="radio_vol", value=.15)
            return "Lauter."
        if has(t, "leiser"):
            await ui(do="radio_vol", value=-.15)
            return "Leiser."
        await ui(do="radio", on=on is not False)
        return "Radio an." if on is not False else "Radio aus."
    if has(t, "timer", "wecker", "erinner"):
        n = first_num(t)
        if n is None:
            return "Wie viele Minuten?"
        secs = n * (3600 if has(t, "stunde") else 60)
        await ui(do="timer", seconds=secs)
        return f"Timer läuft, {n} {'Stunden' if secs >= 3600 else 'Minuten'}."
    pages = {"start": "start", "energie": "energy", "batterie": "batt", "tank": "tanks", "klima": "klima", "niveau": "level",
             "wetter": "wx", "licht": "licht", "radio": "radio", "system": "sys", "karte": "extras", "checkliste": "extras"}
    if has(t, "zeig", "öffne", "seite"):
        for w, pg in pages.items():
            if w in t:
                await ui(do="page", page=pg)
                return "Bitte schön."
    if has(t, "danke"):
        return "Gern geschehen."
    if has(t, "hallo", "hi", "guten"):
        return "Hallo! Was kann ich tun?"
    return "Das habe ich leider nicht verstanden."


def weather_text(t):
    wx = S["state"].get("weather") or {}
    c, d = wx.get("current") or {}, wx.get("daily") or {}
    if not c:
        return "Ich habe gerade keine Wetterdaten."
    place = S["state"].get("weather_place") or ""
    k = 1 if "morgen" in t else 0
    try:
        tmax, tmin = d["temperature_2m_max"][k], d["temperature_2m_min"][k]
        rain = d["precipitation_probability_max"][k]
        gust = d["wind_gusts_10m_max"][k]
    except Exception:
        return f"In {place} sind es {fmt(c.get('temperature_2m', 0))} Grad."
    if k:
        return (f"Morgen in {place}: {fmt(tmin)} bis {fmt(tmax)} Grad, Regenwahrscheinlichkeit {rain} Prozent"
                + (f", Böen bis {fmt(gust)} Kilometer pro Stunde." if gust and gust >= 40 else "."))
    return (f"In {place} sind es {fmt(c.get('temperature_2m', 0))} Grad. Heute {fmt(tmin)} bis {fmt(tmax)} Grad, "
            f"Regenwahrscheinlichkeit {rain} Prozent" + (f", Böen bis {fmt(gust)} Kilometer pro Stunde." if gust and gust >= 40 else "."))


# ---------------------------------------------------------------------------
async def handle(cmd, ws):
    t = cmd.get("type", "")
    if t == "voice_get":
        await ws.send_str(json.dumps(status()))
    elif t == "voice_setup":
        asyncio.get_running_loop().create_task(setup_voice())
    elif t == "voice_enable":
        cfg()["enabled"] = bool(cmd.get("on"))
        S["save_config"]()
        if cfg()["enabled"]:
            await start_listening()
        else:
            stop_listening()
            V["state"] = "aus"
        await push_status()
    elif t == "voice_listen":
        await listen_now()
    elif t == "voice_text":           # Befehl getippt / zum Testen
        await on_text(str(cmd.get("text", ""))[:200] if _wake_in(str(cmd.get("text", ""))) else
                      f"{cfg()['wake'][0]} {cmd.get('text', '')}")
    elif t == "voice_speak":
        cfg()["speak"] = bool(cmd.get("on"))
        S["save_config"]()
        await push_status()
    elif t == "voice_test":
        await speak("Hallo, ich bin die Womo-Zentrale.")
    else:
        return False
    return True


def start():
    cfg()
    if cfg()["enabled"]:
        asyncio.get_running_loop().create_task(start_listening())


def shutdown():
    stop_listening()
