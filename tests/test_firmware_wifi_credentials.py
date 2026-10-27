"""R1 firmware Wi-Fi credential regression guards (source-level only).

These checks intentionally do not compile or execute ESP32 firmware. Real NVS
migration, Wi-Fi join, captive portal and display require hardware validation.
"""
import re
import unittest
from pathlib import Path

FIRMWARE = (
    Path(__file__).resolve().parents[1]
    / "esp32" / "ink-display-wifi-epd" / "ink-display-wifi-epd.ino"
)


def function_body(source: str, name: str) -> str:
    """Return a function's C++ body for narrow source-level invariant checks."""
    match = re.search(r"^\s*(?:static\s+)?void\s+" + re.escape(name) + r"\s*\([^;]*?\)\s*\{", source, re.S | re.M)
    if not match:
        raise AssertionError(f"missing function: {name}")
    # Only use this helper on simple sections without braces in quoted strings.
    start = match.end()
    depth = 1
    for i in range(start, len(source)):
        if source[i] == "{":
            depth += 1
        elif source[i] == "}":
            depth -= 1
            if depth == 0:
                return source[start:i]
    raise AssertionError(f"unclosed function: {name}")


class FirmwareCredentialPolicyTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.source = FIRMWARE.read_text(encoding="utf-8")

    def test_no_builtin_wifi_credentials_or_autoseed(self):
        for forbidden in (
            "BUILTIN_WIFI_SSID", "BUILTIN_WIFI_PASS",
            "ensureBuiltinWifiProfile", "固件内置备用 WiFi 已写入",
        ):
            with self.subTest(forbidden=forbidden):
                self.assertNotIn(forbidden, self.source)

    def test_legacy_nvs_slots_still_loaded(self):
        body = function_body(self.source, "loadConfig")
        for slot in range(3):
            suffix = "" if slot == 0 else str(slot)
            for key in ("ssid", "pass"):
                self.assertRegex(
                    body,
                    r'prefs\.getString\("' + key + suffix + r'"\s*,\s*""\)',
                )
        self.assertIn('prefs.begin("dashcfg", true)', body)
        self.assertNotIn("prefs.put", body)
        self.assertNotIn("prefs.clear", body)

    def test_user_config_save_still_persists_three_slots(self):
        body = function_body(self.source, "saveConfig")
        self.assertIn("i < WIFI_PROFILE_COUNT", body)
        self.assertIn("prefs.putString(ks.c_str(), cfg.wifi_ssid[i])", body)
        self.assertIn("prefs.putString(kp.c_str(), cfg.wifi_pass[i])", body)

    def test_boot_uses_saved_config_or_ap_portal(self):
        boot = function_body(self.source, "setup")
        self.assertIn("loadConfig(g_cfg);", boot)
        self.assertRegex(boot, r"if\s*\(\s*!g_cfg\.valid\s*\|\|\s*!validDownloadKey\(g_cfg\.download_key\)\s*\)\s*\{")
        self.assertIn("startConfigPortal();", boot)
        self.assertLess(boot.index("loadConfig(g_cfg);"), boot.index("if (!g_cfg.valid || !validDownloadKey(g_cfg.download_key))"))
        self.assertNotIn("prefs.putString", boot)

    def test_factory_reset_clears_nvs_without_reseeding(self):
        reset = function_body(self.source, "clearConfigNVS")
        boot = function_body(self.source, "setup")
        self.assertIn("prefs.clear();", reset)
        self.assertIn("clearConfigNVS();", boot)
        self.assertLess(boot.index("clearConfigNVS();"), boot.index("loadConfig(g_cfg);"))

    def test_no_shared_ap_password(self):
        self.assertNotIn('"12345678"', self.source)
        self.assertIn("WiFi.softAP(apSsid.c_str(), g_apPassword.c_str())", self.source)

    def test_ap_password_is_generated_and_stored_in_nvs(self):
        self.assertIn('prefs.getString("ap_pwd", "")', self.source)
        self.assertIn('prefs.putString("ap_pwd", password)', self.source)
        self.assertIn("esp_random()", self.source)
        self.assertIn("snprintf(generated, sizeof(generated)", self.source)

    def test_random_ap_password_after_wifi_radio_is_enabled(self):
        body = function_body(self.source, "startConfigPortal")
        self.assertLess(body.index("wifiHardResetForPortal();"),
                        body.index("g_apPassword = loadOrCreateApPassword();"))
        self.assertLess(body.index("g_apPassword = loadOrCreateApPassword();"),
                        body.index("showNetworkInfoScreen(true, false);"))
        self.assertLess(body.index("showNetworkInfoScreen(true, false);"),
                        body.index("WiFi.softAP("))

    def test_ap_nvs_failure_does_not_start_open_ap(self):
        self.assertIn('if (!prefs.begin("dashcfg", false))', self.source)
        self.assertIn('if (prefs.putString("ap_pwd", password) != password.length())', self.source)
        body = function_body(self.source, "startConfigPortal")
        self.assertIn("if (g_apPassword.length() != 16)", body)
        self.assertIn("goDeepSleepMinutes(30);", body)

    def test_ap_screen_shows_current_secret_and_factory_reset_clears_it(self):
        display = function_body(self.source, "showNetworkInfoScreen")
        self.assertIn('("Pass: " + g_apPassword).c_str()', display)
        reset = function_body(self.source, "clearConfigNVS")
        self.assertIn('prefs.begin("dashcfg", false)', reset)
        self.assertIn("prefs.clear();", reset)

    def test_download_key_not_embedded_in_firmware(self):
        self.assertNotIn("DAILY_PHOTO_PATH_PREFIX", self.source)
        self.assertIn('cfg.download_key     = prefs.getString("dl_key", "")', self.source)
        self.assertIn('prefs.putString("dl_key", key)', self.source)
        self.assertIn('"/static/inktime/" + cfg.download_key + "/"', self.source)

    def test_download_key_grammar_and_fail_closed(self):
        self.assertIn("key.length() < 24 || key.length() > 128", self.source)
        self.assertIn("if (!validDownloadKey(cfg.download_key)) return", self.source)
        boot = function_body(self.source, "setup")
        self.assertIn("!validDownloadKey(g_cfg.download_key)", boot)
        self.assertIn("startConfigPortal();", boot)

    def test_download_key_ap_only_provisioning(self):
        handler = function_body(self.source, "handleSave")
        self.assertIn('server.arg("dl_key")', handler)
        self.assertIn("if (!g_configPortalActive)", handler)
        self.assertIn("server.send(403", handler)
        self.assertIn("if (!saveDownloadKey(submittedKey))", handler)
        self.assertIn("server.send(500", handler)
        self.assertLess(handler.index("saveDownloadKey(submittedKey)"), handler.index("saveConfig(newCfg)"))
        portal = function_body(self.source, "startConfigPortal")
        self.assertIn("g_configPortalActive = apOk;", portal)

    def test_download_key_never_reflected_in_form_or_status(self):
        self.assertNotIn("htmlEscape(g_cfg.download_key)", self.source)
        self.assertNotIn("html += g_cfg.download_key", self.source)
        status = function_body(self.source, "handleDeviceStatus")
        self.assertNotIn("download_key", status)
        self.assertNotIn("dl_key", status)

    def test_download_key_url_not_logged_in_firmware(self):
        self.assertNotIn("url.c_str()", self.source)
        self.assertNotIn(' + " " + url', self.source)
        self.assertNotIn(' + " | GET " + url', self.source)
        self.assertIn('journalSetServer(g_cfg.backend_hostport,', self.source)

    def test_device_journal_does_not_log_bearer_url(self):
        journal = (FIRMWARE.parent / "device_log.cpp").read_text(encoding="utf-8")
        self.assertNotIn("url.c_str()", journal)
        self.assertIn('url += "/api/device_log/" + s_key;', journal)
        self.assertIn("JLOG(\"[DEVLOG] 上传开始 cur=%u（URL 密钥已隐藏）", journal)

    def test_ap_configuration_entrypoints_retained(self):
        portal = function_body(self.source, "startConfigPortal")
        self.assertIn("WiFi.softAP(", portal)
        self.assertIn('server.on("/network",', portal)
        self.assertIn('server.on("/save",', portal)
        self.assertIn("server.handleClient();", portal)


if __name__ == "__main__":
    unittest.main()
