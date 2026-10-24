# ATMO

A Raspberry Pi 5 + barometer payload for drones. It turns air pressure into live altitude, climb rate, turbulence and hover quality, and shows it all on a dashboard that works with no internet.

Built at Cal Hacks 13.0.
<img width="1408" height="790" alt="Screenshot 2026-10-24 at 1 49 47 AM" src="https://github.com/user-attachments/assets/60c8e020-3b93-4041-a883-c653983cbdea" />



## What it does
- Live height above your start point, vertical speed, and a 20 min pressure trace
- Flight metrics: air disturbance, hover quality, max climb and sink, total ascent and descent, time aloft
- Pressure trend outlook (rule of thumb, not a weather model)
- Logs every session to CSV
- No wifi needed: the Pi falls back to its own `ATMO` hotspot and serves the dashboard at `10.42.0.1:8000`

## Hardware
- Raspberry Pi 4
- Grove Base HAT
- Grove Barometer SPA06-003 (I2C 0x77)
- Optional Grove 16x2 LCD (skipped automatically if missing)

<img width="4032" height="2268" alt="IMG_0521" src="https://github.com/user-attachments/assets/f7324045-07b4-4289-bba4-43506dda6428" />
<img width="4032" height="2268" alt="IMG_0522" src="https://github.com/user-attachments/assets/eaded780-c06c-49fd-ab1c-c06693c25ab8" />


https://github.com/user-attachments/assets/5c019709-9b06-4002-9f74-1fdf05a27e0d




## Setup
On the Pi:
```bash
git clone https://github.com/faisalshariff123/ATMO.git
cd ATMO
bash deploy/setup.sh
```
Set your own hotspot password in `deploy/setup.sh` first. Then open `http://<pi-ip>:8000`.

Manual run:
```bash
python -m venv --system-site-packages .
./bin/pip install -r requirements.txt
./bin/uvicorn app:app --host 0.0.0.0 --port 8000
```

## Files
- `app.py`: sensor sampling, filtering, metrics, API
- `static/index.html`: the dashboard (one file, no CDN)
- `deploy/`: systemd service and setup script

## Limits
- Wind and prop wash spike the pressure reading, so shield the sensor
- The sensor's temperature reads chip heat, not air, so it isn't used
- The Pi has no clock battery, so offline timestamps can be off
