"""Fail-closed validation for HTTP callbacks to registered ESP32 devices.

Only literal RFC1918 IPv4 addresses are accepted. Do not trust X-Forwarded-For
or permit arbitrary DNS names, loopback, link-local, public, or IPv6 targets.
This is a LAN egress guard, not device authentication or network isolation.
"""
from ipaddress import IPv4Address, IPv4Network

_LAN_NETWORKS = (
    IPv4Network("10.0.0.0/8"),
    IPv4Network("172.16.0.0/12"),
    IPv4Network("192.168.0.0/16"),
)


def validate_device_endpoint(ip: object, port: object = 80) -> tuple[str, int]:
    """Return canonical (IPv4, port) or reject the entire endpoint.

    The port is optional only when omitted/empty; an explicit invalid value
    must not silently fall back to 80.
    """
    if not isinstance(ip, str):
        raise ValueError("device IP must be an IPv4 string")
    try:
        address = IPv4Address(ip.strip())
    except ValueError as exc:
        raise ValueError("invalid device IPv4 address") from exc
    if not any(address in subnet for subnet in _LAN_NETWORKS):
        raise ValueError("device address must be on an RFC1918 LAN")

    if port is None or port == "":
        port = 80
    if isinstance(port, bool) or not isinstance(port, (str, int)):
        raise ValueError("invalid device port")
    if isinstance(port, str):
        if not port.isascii() or not port.isdecimal() or len(port) > 5:
            raise ValueError("invalid device port")
        port = int(port)
    if not 1 <= port <= 65535:
        raise ValueError("device port out of range")
    return str(address), port


def resolve_registration_allowed_ip(environ) -> str | None:
    """Optional exact LAN peer pin for device auto-registration.

    This limits shared-key takeover from other peer addresses, but is NOT
    cryptographic device identity or protection against LAN address spoofing.
    Empty/unset means legacy behavior; malformed nonempty settings fail startup.
    """
    value = environ.get("INKTIME_DEVICE_REGISTRATION_ALLOWED_IP")
    if value is None or value == "":
        return None
    if not isinstance(value, str) or value != value.strip():
        raise ValueError("registration allowed IP must be a canonical RFC1918 IPv4 address")
    ip, _ = validate_device_endpoint(value)
    if ip != value:
        raise ValueError("registration allowed IP must be canonical")
    return ip
