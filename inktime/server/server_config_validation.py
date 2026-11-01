"""Fail-closed validation of core InkTime server configuration.

This module deliberately does not import config.py or touch the filesystem.
"""
from pathlib import Path
import ipaddress


class ConfigurationError(ValueError):
    pass


def validate_server_settings(config):
    """Return normalized settings; reject invalid types before side effects."""
    def path(name):
        value = getattr(config, name, None)
        if not isinstance(value, (str, Path)) or not str(value).strip():
            raise ConfigurationError(f"{name} must be a non-empty path")
        return Path(value).expanduser()

    def integer(name, default, minimum, maximum):
        value = getattr(config, name, default)
        if type(value) is not int or not minimum <= value <= maximum:
            raise ConfigurationError(f"{name} must be an integer in [{minimum}, {maximum}]")
        return value

    host = getattr(config, "FLASK_HOST", "0.0.0.0")
    if not isinstance(host, str):
        raise ConfigurationError("FLASK_HOST must be a literal IPv4 address")
    try:
        ipaddress.IPv4Address(host)
    except ipaddress.AddressValueError:
        raise ConfigurationError("FLASK_HOST must be a literal IPv4 address") from None

    enabled = getattr(config, "ENABLE_REVIEW_WEBUI", True)
    if type(enabled) is not bool:
        raise ConfigurationError("ENABLE_REVIEW_WEBUI must be boolean")

    return {
        "DB_PATH": path("DB_PATH"),
        "IMAGE_DIR": path("IMAGE_DIR"),
        "BIN_OUTPUT_DIR": path("BIN_OUTPUT_DIR"),
        "FLASK_HOST": host,
        "FLASK_PORT": integer("FLASK_PORT", 8765, 1, 65535),
        "ENABLE_REVIEW_WEBUI": enabled,
        "DAILY_PHOTO_QUANTITY": integer("DAILY_PHOTO_QUANTITY", 5, 1, 100),
        "SIM_RENDER_CACHE_MAX_ENTRIES": integer("SIM_RENDER_CACHE_MAX_ENTRIES", 300, 1, 100000),
    }
