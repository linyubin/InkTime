# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

InkTime is an AI-powered e-paper photo frame that analyzes your photo library using vision language models (VLM), scores photos by "memorability" and aesthetics, and displays "photos from this day in history" on e-paper displays.

The project consists of three main components:

1. **Python Backend** (`analyze_photos.py`, `render_daily_photo.py`, `server.py`, `webui.py`):
   - Analyzes photos using VLM APIs (OpenLM-compatible: LM Studio, Ollama, etc.)
   - Renders selected photos for e-paper displays
   - Provides web UI for photo review and ESP32 download server

2. **ESP32 Firmware** (`esp32/ink-display-7C-photo/`, `esp32/ink-display-133C-photo/`):
   - ESP32-S3-based e-paper display devices
   - WiFi connectivity to pull rendered images from server
   - Deep sleep for battery efficiency

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
- **Board**: ESP32-S3 Dev Module (PSRAM required)
  - Tools > PSRAM: OPI PSRAM
- **Required Libraries**: GxEPD2
- **Firmware Paths**:
  - 7.3" display: `esp32/ink-display-7C-photo/ink-display-7C-photo.ino`
  - 1.33" display: `esp32/ink-display-133C-photo/ink-display-133C-photo.ino`

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
- `output/` - Rendered `.bin` files
- `templates/` - Flask/Jinja2 templates for WebUI
- `scripts/daily_render.sh` - Cron job script (render + BLE push)
- `docker/` - Container deployment files
- `esp32/` - ESP32 firmware projects
  - `ink-display-7C-photo/` - 7.3" display firmware
  - `ink-display-133C-photo/` - 1.33" display firmware
  - `pcb/` - Hardware design files
- `EPD-nRF5/` - Nordic nRF5-based display with Web Bluetooth control

## Development Notes

- All prompt templates for VLM are in `analyze_photos.py` functions
- Exif/GPS extraction falls back to filename date parsing if EXIF unavailable
- BLE push requires Linux/Unix with BlueZ stack (or Windows with bleak)
- ESP32 firmware uses GxEPD2 library for display control
- EPD-nRF5 includes a Windows emulator (`emulator.c`) for offline testing
- Path mapping available via `PATH_MAP` for cross-platform deployments
- NAS mount retry logic in `analyze_photos.py` for network photo storage

## Testing

Test scripts in `scripts/`:
- `test_ollama.py` - Test VLM connectivity
- `test_exiftool.py` - Test EXIF extraction
- `test_bin_convert.py` - Test binary rendering
- `debug_*.py` - Various debugging utilities
