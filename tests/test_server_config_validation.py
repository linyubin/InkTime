import unittest
from types import SimpleNamespace
from pathlib import Path
from server_config_validation import ConfigurationError, validate_server_settings


def valid(**overrides):
    settings = dict(DB_PATH="./photos.db", IMAGE_DIR="./photos", BIN_OUTPUT_DIR="./output",
                    FLASK_HOST="127.0.0.1", FLASK_PORT=8765,
                    ENABLE_REVIEW_WEBUI=True, DAILY_PHOTO_QUANTITY=5,
                    SIM_RENDER_CACHE_MAX_ENTRIES=300)
    settings.update(overrides)
    return SimpleNamespace(**settings)


class ServerConfigValidationTests(unittest.TestCase):
    def test_valid(self):
        self.assertEqual(validate_server_settings(valid())["FLASK_PORT"], 8765)

    def test_path_types_and_empty(self):
        for value in ("", "  ", None, 3, True):
            with self.subTest(value=value), self.assertRaises(ConfigurationError):
                validate_server_settings(valid(DB_PATH=value))

    def test_port_bounds(self):
        for value in (0, 65536, "8765", True, -1):
            with self.subTest(value=value), self.assertRaises(ConfigurationError):
                validate_server_settings(valid(FLASK_PORT=value))

    def test_host_must_be_literal_ipv4(self):
        for value in ("localhost", "::1", "example.com", "", 5):
            with self.subTest(value=value), self.assertRaises(ConfigurationError):
                validate_server_settings(valid(FLASK_HOST=value))

    def test_boolean_not_coerced(self):
        for value in ("false", 0, 1, None):
            with self.subTest(value=value), self.assertRaises(ConfigurationError):
                validate_server_settings(valid(ENABLE_REVIEW_WEBUI=value))

    def test_quantity_and_cache_bounds(self):
        for key in ("DAILY_PHOTO_QUANTITY", "SIM_RENDER_CACHE_MAX_ENTRIES"):
            with self.subTest(key=key), self.assertRaises(ConfigurationError):
                validate_server_settings(valid(**{key: 0}))

    def test_accepts_path_objects(self):
        self.assertEqual(validate_server_settings(valid(DB_PATH=Path("photos.db")))["DB_PATH"], Path("photos.db"))

    def test_server_gate_before_db_preflight(self):
        source = Path(__file__).resolve().parents[1].joinpath("server.py").read_text(encoding="utf-8")
        self.assertLess(source.index("SERVER_SETTINGS = validate_server_settings(cfg)"), source.index("require_current_schema(DB_PATH)"))


if __name__ == "__main__":
    unittest.main()
