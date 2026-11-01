# Vendored: ESP32epdx v1.0.9

- Source: https://github.com/gooddisplayshare/ESP32epdx (GoodDisplay official share repo, library.properties url)
- Pulled 2026-11-01 from operator PC Arduino library install (Documents\\Arduino\\libraries\\ESP32epdx).
- Examples/ stripped to keep the repo lean (8.9MB of panel demo images); full version at the URL above.
- Why vendored: firmware `esp32/ink-display-wifi-epd` includes <ESP32epdx.h> and its EPD.cpp
  includes <EPD_SPI.h>; without this library in git the firmware build is not reproducible
  (verification finding H1-BLOCKER). Compile with `arduino-cli compile --libraries esp32/libraries`.
