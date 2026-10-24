import csv, json, os, statistics, subprocess, threading, time, urllib.request
from collections import deque
from datetime import datetime

import board
from adafruit_spa06_003 import SPA06_003
from fastapi import FastAPI
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

HERE = os.path.dirname(os.path.abspath(__file__))
LOGDIR = os.path.join(HERE, "logs")
os.makedirs(LOGDIR, exist_ok=True)
LOGFILE = os.path.join(LOGDIR, datetime.now().strftime("session-%Y%m%d-%H%M%S.csv"))

KEY = os.environ.get("GEMINI_API_KEY", "")
MODEL = os.environ.get("GEMINI_MODEL", "gemini-3.8-flash")

spa = SPA06_003.over_i2c(board.I2C(), address=0x77)
lock = threading.Lock()
series = deque(maxlen=2400)   # 0.5 s samples, 20 min
raw = deque(maxlen=10)        # raw pressure, about 2 s at 5 Hz
minutes = deque(maxlen=180)   # per-minute mean pressure, 3 h


def fresh_metrics():
    return {"max": 0.0, "min": 0.0, "vs": 0.0, "vmax": 0.0, "vmin": 0.0,
            "up": 0.0, "down": 0.0, "ref": None, "aloft": 0.0,
            "dist": 0.0, "dist_max": 0.0, "hover": None, "hover_best": None}


S = {"base": None, "ema": None, "alt": 0.0, "p": None, "temp": None, "t": 0.0,
     "n": 0, "t0": time.time(), "ok": False}
S.update(fresh_metrics())
acc = {"sum": 0.0, "n": 0, "t": time.time()}
summ = {"t": 0.0, "text": "", "source": ""}

DEADBAND = 0.05      # m, ignore wiggles smaller than this when totalling ascent/descent
ALOFT_M = 1.0        # m above the zero point that counts as aloft
HOLD_VS = 0.15       # m/s, below this the sensor counts as holding still


def avg_pressure(n=20):
    s = 0.0
    for _ in range(n):
        s += spa.pressure
        time.sleep(0.1)
    return s / n


def update_metrics(now, dt, p, alt):
    """Runs under the lock, once per raw sample."""
    dist = (max(raw) - min(raw)) if len(raw) >= 5 else 0.0
    S["dist"] = dist
    S["dist_max"] = max(S["dist_max"], dist)
    S["max"] = max(S["max"], alt)
    S["min"] = min(S["min"], alt)

    # vertical speed over about 3 s of smoothed height
    vs = 0.0
    if len(series) >= 2:
        i = len(series) - 1
        while i > 0 and now - series[i][0] < 3.0:
            i -= 1
        span = now - series[i][0]
        if span >= 1.0:
            vs = (alt - series[i][1]) / span
    S["vs"] = vs
    if S["n"] > 30:  # let the filter settle first
        S["vmax"] = max(S["vmax"], vs)
        S["vmin"] = min(S["vmin"], vs)

    # total ascent and descent with a deadband so noise does not add up
    ref = S["ref"]
    if ref is None:
        S["ref"] = alt
    elif alt - ref > DEADBAND:
        S["up"] += alt - ref
        S["ref"] = alt
    elif ref - alt > DEADBAND:
        S["down"] += ref - alt
        S["ref"] = alt

    if alt > ALOFT_M:
        S["aloft"] += dt

    # hover quality: spread of height over the last 10 s while holding still (mine probably sucks cuz of the way its built)
    S["hover"] = None
    if len(series) >= 20 and alt > 0.5 and abs(vs) < HOLD_VS:
        recent = [r[1] for r in list(series)[-20:]]
        if max(recent) - min(recent) < 0.6:
            sd = statistics.pstdev(recent)
            S["hover"] = sd
            if S["hover_best"] is None or sd < S["hover_best"]:
                S["hover_best"] = sd


def sampler():
    time.sleep(0.5)
    base = avg_pressure()
    with lock:
        S["base"] = base
    f = open(LOGFILE, "w", newline="")
    w = csv.writer(f)
    w.writerow(["t", "chip_temp_c", "pressure_hpa", "rel_alt_m", "vs_mps", "disturbance_hpa"])
    flushed = time.time()
    last_rec = 0.0
    prev = time.time()
    while True:
        try:
            p = spa.pressure
            tc = spa.temperature
        except Exception:
            with lock:
                S["ok"] = False
            time.sleep(0.5)
            continue
        now = time.time()
        dt = min(now - prev, 1.0)
        prev = now
        row = None
        with lock:
            raw.append(p)
            med = statistics.median(list(raw)[-5:])   # median of 5 rejects short gust spikes
            S["ema"] = med if S["ema"] is None else S["ema"] + 0.25 * (med - S["ema"])
            alt = 44330 * (1 - (S["ema"] / S["base"]) ** 0.1903)
            S.update(alt=alt, p=S["ema"], temp=tc, t=now, ok=True, n=S["n"] + 1)
            update_metrics(now, dt, p, alt)
            acc["sum"] += p
            acc["n"] += 1
            if now - acc["t"] >= 60:
                minutes.append((now, acc["sum"] / acc["n"]))
                acc.update(sum=0.0, n=0, t=now)
            if now - last_rec >= 0.5:
                last_rec = now
                series.append((now, alt, S["ema"], tc))
                row = [f"{now:.2f}", f"{tc:.2f}", f"{p:.3f}", f"{alt:.3f}",
                       f"{S['vs']:.3f}", f"{S['dist']:.4f}"]
        if row:
            w.writerow(row)
            if now - flushed > 5:
                f.flush()
                flushed = now
        time.sleep(0.2)


threading.Thread(target=sampler, daemon=True).start()


def trend():
    with lock:
        m = list(minutes)
    if len(m) < 5:
        return None, len(m)
    (t0, p0), (t1, p1) = m[0], m[-1]
    return (p1 - p0) / ((t1 - t0) / 3600.0) * 3.0, len(m)


def forecast(r3, p):
    if r3 is None:
        return "Calibrating", "Collecting pressure history. A trend needs about 5 minutes.", "idle"
    if r3 <= -3.6:
        return "Storm risk", "Pressure is falling fast. Wind and rain are likely within hours.", "bad"
    if r3 <= -1.6:
        return "Deteriorating", "Pressure is falling. Clouds and rain are getting more likely.", "warn"
    if r3 >= 3.6:
        return "Clearing fast", "Pressure is rising quickly. Clearing skies, possibly gusty.", "good"
    if r3 >= 1.6:
        return "Improving", "Pressure is rising. Drier, clearer air is on the way.", "good"
    if p >= 1022:
        return "Fair and steady", "High, steady pressure. Settled weather is likely.", "good"
    if p <= 1009:
        return "Unsettled", "Low but steady pressure. Changeable weather is possible.", "warn"
    return "Steady", "Pressure is steady. No big change expected soon.", "good"


def snapshot():
    r3, nm = trend()
    with lock:
        s = dict(S)
    if s["p"] is None:
        return None
    title, text, tone = forecast(r3, s["p"])
    return {"ready": True, "alt": s["alt"], "p": s["p"], "temp": s["temp"], "ok": s["ok"],
            "max": s["max"], "min": s["min"], "n": s["n"], "dur": time.time() - s["t0"],
            "vs": s["vs"], "vmax": s["vmax"], "vmin": s["vmin"],
            "up": s["up"], "down": s["down"], "aloft": s["aloft"], "airborne": s["alt"] > ALOFT_M,
            "dist": s["dist"], "dist_max": s["dist_max"],
            "hover": s["hover"], "hover_best": s["hover_best"],
            "rate3": r3, "minutes": nm, "fc": {"title": title, "text": text, "tone": tone},
            "log": os.path.basename(LOGFILE)}


try:
    import smbus2
except ImportError:
    smbus2 = None

LCD_ADDR = 0x3E
RGB_ADDRS = (0x62, 0x30)   # RGB-backlight versions only: v4 uses 0x62, v5 uses 0x30


class Lcd:
    def __init__(self, bus=1):
        self.bus = smbus2.SMBus(bus)
        self.bus.write_byte_data(LCD_ADDR, 0x80, 0x28)   # raises OSError if no LCD is attached
        self.rgb = None
        for a in RGB_ADDRS:
            try:
                self.bus.write_byte_data(a, 0x00, 0)
                self.rgb = a
                break
            except OSError:
                pass
        time.sleep(0.05)
        for c in (0x28, 0x28, 0x0C, 0x01):
            self.cmd(c)
            time.sleep(0.003)
        self.cmd(0x06)
        self.color = None
        self.rows = ["", ""]

    def cmd(self, c):
        self.bus.write_byte_data(LCD_ADDR, 0x80, c)

    def set_color(self, rgb):
        if not self.rgb or rgb == self.color:
            return
        self.color = rgb
        r, g, b = rgb
        for reg, val in ((0x00, 0), (0x01, 0), (0x08, 0xAA), (0x04, r), (0x03, g), (0x02, b)):
            self.bus.write_byte_data(self.rgb, reg, val)

    def row(self, i, text):
        text = text[:16].ljust(16)
        if text == self.rows[i]:
            return
        self.rows[i] = text
        self.cmd(0x80 | (0x40 * i))
        for ch in text:
            self.bus.write_byte_data(LCD_ADDR, 0x40, ord(ch) if ord(ch) < 128 else 63)


def lcd_lines(sn):
    d = sn["dist"]
    if d < 0.03:
        word, col = "calm", (0, 160, 60)
    elif d < 0.1:
        word, col = "light", (200, 200, 200)
    elif d < 0.3:
        word, col = "rough", (255, 140, 0)
    else:
        word, col = "gust!", (255, 0, 0)
    r0 = f"{sn['alt']:+7.2f}m {sn['vs']:+5.2f}/s"
    r1 = f"{sn['p']:7.1f}hPa {word:>5}"
    return r0, r1, col


def lcd_ips():
    try:
        out = subprocess.check_output(["hostname", "-I"], timeout=2).decode().split()
        return [a for a in out if a.count(".") == 3][:3] or ["no network"]
    except Exception:
        return ["no network"]


def lcd_loop():
    if smbus2 is None:
        return
    try:
        lcd = Lcd()
    except Exception:
        return   # no LCD attached, carry on without it
    lcd.set_color((0, 120, 255))
    lcd.row(0, "ATMO dashboard")
    for ip in lcd_ips():
        lcd.row(1, ip)
        time.sleep(2)
    while True:
        try:
            sn = snapshot()
            if sn is None:
                lcd.row(0, "ATMO warming up")
                lcd.row(1, "")
            else:
                r0, r1, col = lcd_lines(sn)
                lcd.set_color(col)
                lcd.row(0, r0)
                lcd.row(1, r1)
        except Exception:
            time.sleep(1)
        time.sleep(0.5)


threading.Thread(target=lcd_loop, daemon=True).start()



def local_summary(sn):
    a = sn["alt"]
    r = sn["rate3"]
    tr = "No pressure trend yet." if r is None else f"The pressure trend is {r:+.1f} hPa per 3 hours."
    return (f"The sensor is {abs(a):.1f} m {'above' if a >= 0 else 'below'} where you zeroed it, "
            f"and the air pressure is {sn['p']:.1f} hPa. {tr} {sn['fc']['text']}")


def gemini(prompt):
    body = json.dumps({"contents": [{"parts": [{"text": prompt}]}]}).encode()
    req = urllib.request.Request(
        f"https://generativelanguage.googleapis.com/v1beta/models/{MODEL}:generateContent",
        data=body, headers={"Content-Type": "application/json", "x-goog-api-key": KEY})
    with urllib.request.urlopen(req, timeout=6) as r:
        j = json.load(r)
    return j["candidates"][0]["content"]["parts"][0]["text"].strip()


app = FastAPI()
app.mount("/static", StaticFiles(directory=os.path.join(HERE, "static")), name="static")


@app.get("/")
def index():
    return FileResponse(os.path.join(HERE, "static", "index.html"))


@app.get("/api/state")
def state(w: int = 120):
    sn = snapshot()
    if sn is None:
        return {"ready": False}
    n = max(2, min(2400, w * 2))
    with lock:
        pts = list(series)[-n:]
    k = max(1, len(pts) // 300)
    pts = pts[::-1][::k][::-1]
    sn["series"] = [[round(t, 2), round(a, 3), round(p, 3), round(tc, 2)] for t, a, p, tc in pts]
    return sn


@app.post("/api/zero")
def zero():
    with lock:
        if S["ema"]:
            S["base"] = S["ema"]
            S.update(fresh_metrics())
            series.clear()
            raw.clear()
    return {"ok": True}


@app.post("/api/summary")
def summary():
    sn = snapshot()
    if sn is None:
        return {"text": "Sensor not ready yet.", "source": "local"}
    if summ["text"] and summ["source"] == "gemini" and time.time() - summ["t"] < 45:
        return {"text": summ["text"], "source": summ["source"]}
    text, src = local_summary(sn), "local"
    if KEY:
        try:
            r = sn["rate3"]
            prompt = ("You are the voice of a small drone weather station. In two short sentences, "
                      "explain these readings in plain English for a non-expert and say what the "
                      "pressure trend suggests. Do not invent numbers. "
                      f"Altitude relative to start: {sn['alt']:.2f} m. Pressure: {sn['p']:.2f} hPa. "
                      f"Trend: {'unknown' if r is None else format(r, '+.1f') + ' hPa per 3 h'}.")
            text, src = gemini(prompt), "gemini"
        except Exception:
            pass
    summ.update(t=time.time(), text=text, source=src)
    return {"text": text, "source": src}


@app.get("/api/log")
def log():
    return FileResponse(LOGFILE, media_type="text/csv", filename=os.path.basename(LOGFILE))
