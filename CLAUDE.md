# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

InkTime is an AI-powered e-paper photo frame that analyzes your photo library using vision language models (VLM), scores photos by "memorability" and aesthetics, and displays "photos from this day in history" on e-paper displays.

The project consists of three main components:

1. **Python Backend** (`analyze_photos.py`, `render_daily_photo.py`, `server.py`, `webui.py`):
   - Analyzes photos using VLM APIs (OpenLM-compatible: LM Studio, Ollama, etc.)
   - Renders selected photos for e-paper displays
   - Provides web UI for photo review and ESP32 download server

2. **ESP32 Firmware** (`esp32/ink-display-wifi-epd/` is the active version; older `ink-display-7C-photo/` / `ink-display-133C-photo/` variants live under `esp32/archive/`; `esp32/servo_serial_cmd/` is a standalone servo test project):
   - WiFi-connected e-paper display devices that pull rendered images from the server
   - AP-mode captive portal for WiFi / server / timezone / refresh-time config (stored in NVS)
   - Deep sleep with daily scheduled wake for battery efficiency
   - `ink-display-wifi-epd/` is the current/active version; `7C-photo` and `133C-photo` are archived older variants under `esp32/archive/`

3. **EPD-nRF5 Integration** (`EPD-nRF5/`, `push_to_epd_ble.py`):
   - Nordic nRF5-based e-paper displays with BLE connectivity
   - Direct BLE push support from NAS/server without WiFi

## Common Commands

### Python Development

```bash
# Setup virtual environment
python3 -m venv venv
source venv/bin/activate  # Linux/macOS
# or
venv\Scripts\activate  # Windows

# Install dependencies
pip install -r requirements.txt

# Analyze photos (scan IMAGE_DIR, call VLM, populate photos.db)
python analyze_photos.py

# Render daily photo (select "today in history" photo, generate .bin)
python render_daily_photo.py

# Start download server + WebUI (access at http://127.0.0.1:8765/review)
python server.py

# Start standalone WebUI manager (port 8766 by default)
python webui.py

# Push to EPD-nRF5 via BLE (requires BLE stack, typically runs on NAS/Linux)
python push_to_epd_ble.py
```

### ESP32 Firmware

- **IDE**: Arduino IDE

Two driver families exist — they target different boards and use different display libraries:

**`ink-display-wifi-epd/`** (current/active; 7.3" 4-color GDEY073D46 / EL073TS3):
- **Board**: ESP32-L Module (classic ESP32). Uses PSRAM when available, falls back to internal RAM.
- **Display driver**: bundled low-level driver (`EPD.cpp` / `EPD.h` — ESP32epdx-style Paint/PIC API, 2 bits/pixel packed 4-color). **Not** GxEPD2.
- **Firmware path**: `esp32/ink-display-wifi-epd/ink-display-wifi-epd.ino`
- **Extras in folder**: schematic (`ESP32-L_SCH.pdf`, `schematic_text.txt`); `convert_img4.py` converts an image to 4-color 2bpp for the status-screen background (`ap_bg.h`).
- **Features beyond the older variants**: draws AP/connection status directly on the e-ink screen; on a manual reset (non-timer wake) it shows network info (SSID/IP/MAC) and keeps the web config server open on the device's LAN IP for ~3 min so settings can be tweaked without re-entering AP mode.
- **Factory reset**: hold GPIO0 (BOOT) at boot → clears NVS and re-enters AP provisioning.

**`esp32/archive/ink-display-7C-photo/` & `esp32/archive/ink-display-133C-photo/`** (archived older variants):
- **Board**: ESP32-S3 Dev Module (PSRAM required; Tools > PSRAM: OPI PSRAM)
- **Display driver**: GxEPD2 (`GxEPD2_7C` / `GxEPD2_730c_GDEY073D46`)
- **Factory reset**: hold GPIO38 at boot.
- **Firmware paths**:
  - 7.3" display: `esp32/archive/ink-display-7C-photo/ink-display-7C-photo.ino`
  - 13.3" display: `esp32/archive/ink-display-133C-photo/ink-display-133C-photo.ino`

### EPD-nRF5 Firmware

- **IDE**: Keil uVision (`.uvprojx` files in `EPD-nRF5/Keil/`)
- **Alternative**: ARM GCC (uses `EPD-nRF5/Makefile`)
- **Web Control**: Open `EPD-nRF5/html/index.html` in browser with Web Bluetooth support

### Docker Deployment

```bash
# Build image
docker build -f docker/Dockerfile -t inktime:latest .

# Run container (example - see docker/walkthrough.md for details)
docker run -d \
  --name inktime \
  --network host \
  -v $(pwd)/config:/config \
  -v /path/to/photos:/photos:ro \
  -v /var/run/dbus:/var/run/dbus \
  inktime:latest
```

Unraid users can use the provided template: `docker/inktime-unraid-template.xml`

## Configuration

All configuration is centralized in `config.py`. Copy from `config-example.py`:

```bash
cp config-example.py config.py
# Edit config.py with your settings
```

**Required settings**:
- `IMAGE_DIR`: Path to your photo library
- `API_URL`, `MODEL_NAME`: VLM API endpoint and model name
- `DOWNLOAD_KEY`: Security prefix for ESP32 downloads (sync with ESP32 firmware)
- `ENABLE_BLE_PUSH` / `ENABLE_ESP32_SERVE`: 投递能力开关（可并存），由 `scripts/inktime_daily.sh` 读取，决定 render 后跑 BLE 推送（nRF5）还是起临时 server 供 ESP32 主动拉取

**Important paths**:
- Database: `photos.db` (SQLite)
- Rendered output: `output/` (contains `.bin` files)
- Logs: `logs/render.log` (from cron jobs)

## Architecture

### Photo Analysis Flow

1. `analyze_photos.py` scans `IMAGE_DIR`
2. Extracts EXIF/GPS data (uses `exiftool` if available)
3. Calls VLM API for each photo:
   - Content analysis (scene, objects, emotions)
   - Scoring: memory_value (1-100), aesthetic_score (1-100)
   - Generates side_caption (one-line text)
4. Stores results in SQLite `photos.photo_scores` table
5. Resumable: already-analyzed photos are skipped

### Daily Rendering Flow

`render_daily_photo.py`:
1. Queries `photos.db` for photos matching today's month-day
2. Applies weighted selection algorithm (considering scores, recency, path/category preferences)
3. Renders selected photo to e-paper format (black/white/red/yellow dithering)
4. Outputs `photo_0.bin` (or multiple based on `DAILY_PHOTO_QUANTITY`)
5. `.bin` format consumed by all ESP32 firmware: portrait 480×800 = 384,000 bytes, 1 byte/pixel, color values `0=black, 1=white, 2=red, 3=yellow`, row-major (y=0..799, x=0..479)

### BLE Push Flow

`push_to_epd_ble.py`:
1. Connects to EPD-nRF5 device via BLE (auto-scan or use `EPD_DEVICE_MAC`)
2. Uses EPD-nRF5 BLE protocol (Service UUID: `62750001-...`)
3. Sends `CMD_WRITE_IMG` (0x30) with binary data
4. Triggers display refresh with `CMD_REFRESH` (0x05)

### Server Endpoints

`server.py` provides:
- `/{DOWNLOAD_KEY}/photo_{n}.bin` - ESP32 download endpoint
- `/{DOWNLOAD_KEY}/latest.bin` - Symlink to latest photo
- `/review` - Web UI for photo review (if `ENABLE_REVIEW_WEBUI=True`)

`webui.py` provides standalone management UI (port 8766):
- Photo browsing with filtering
- Path include/exclude configuration
- Date-based preview
- DataTables interface

## Key Directories

- `analyze_photos.py` - Main photo analysis script
- `render_daily_photo.py` - Daily photo rendering
- `server.py` - Download server + review UI
- `webui.py` - Standalone web UI manager
- `push_to_epd_ble.py` - BLE push to EPD-nRF5
- `config.py` - Project configuration (copy from config-example.py)
- `config-example.py` - Configuration template
- `photos.db` - SQLite database (auto-created)
- `output/` - Rendered `.bin` files (由 server.py 在 `/static/inktime/<DOWNLOAD_KEY>/` 下提供下载)
- `logs/transfer.log` - 详细 HTTP 传输日志（ESP32 每次拉取：时间/IP/idx/字节数/耗时）；另有 `logs/render.log`（cron 脚本日志）
- `logs/device/<key>.log` - ESP32 设备日志（黑盒，JSONL）；查看页 WebUI `/devlog`，设计见 `docs/esp32-device-log.md` 与 `docs/adr/0001`
- `tmp/last_fetch.json` - 拉取成功哨兵，供 `inktime_daily.sh` 轮询得知 ESP32 已取走数据
- `templates/` - Flask/Jinja2 templates for WebUI
- `scripts/inktime_daily.sh` - 每日编排：render → 按 `ENABLE_BLE_PUSH`/`ENABLE_ESP32_SERVE` 跑 BLE 推送 / 起临时 server 供 ESP32 拉取（检测到拉取或超时后关闭）
- `docker/` - Container deployment files
- `esp32/` - ESP32 firmware projects
  - `ink-display-wifi-epd/` - Current/active 7.3" WiFi EPD firmware (ESP32-L, bundled EPD driver, on-screen status + manual-wake LAN config)
  - `archive/ink-display-7C-photo/` - Archived older 7.3" display firmware (ESP32-S3, GxEPD2)
  - `archive/ink-display-133C-photo/` - Archived older 13.3" display firmware (ESP32-S3, GxEPD2)
  - `servo_serial_cmd/` - Standalone serial servo test/calibration project
  - `pcb/` - Hardware design files
- `EPD-nRF5/` - Nordic nRF5-based display with Web Bluetooth control

## Development Notes

- All prompt templates for VLM are in `analyze_photos.py` functions
- Exif/GPS extraction falls back to filename date parsing if EXIF unavailable
- BLE push requires Linux/Unix with BlueZ stack (or Windows with bleak)
- ESP32 display drivers differ by firmware: `ink-display-wifi-epd/` uses the bundled `EPD.h` low-level driver (ESP32epdx-style Paint/PIC API, 2 bits/pixel), while the archived `archive/ink-display-7C-photo` / `archive/ink-display-133C-photo` variants use GxEPD2.
- EPD-nRF5 includes a Windows emulator (`emulator.c`) for offline testing
- Path mapping available via `PATH_MAP` for cross-platform deployments
- NAS mount retry logic in `analyze_photos.py` for network photo storage

## Testing

Test scripts in `scripts/`:
- `test_ollama.py` - Test VLM connectivity
- `test_exiftool.py` - Test EXIF extraction
- `test_bin_convert.py` - Test binary rendering
- `debug_*.py` - Various debugging utilities
