"""R1: device endpoint egress validation and real route-body regression tests.

Extract selected function definitions from server.py without importing its
production side effects. No network, real config, database, or device is used.
"""
import ast
import copy
import json
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

from flask import Flask, abort, jsonify, request

from device_endpoint_security import validate_device_endpoint
from download_key_security import authorized_download_key

SERVER = Path(__file__).resolve().parents[1] / "server.py"


def load_server_functions(names, namespace):
    """Execute unchanged server.py function bodies in an isolated namespace."""
    tree = ast.parse(SERVER.read_text(encoding="utf-8"))
    functions = []
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name in names:
            item = copy.deepcopy(node)
            item.decorator_list = []
            functions.append(item)
    if {f.name for f in functions} != set(names):
        raise AssertionError("server.py endpoint function missing")
    exec(compile(ast.fix_missing_locations(ast.Module(body=functions, type_ignores=[])),
                 str(SERVER), "exec"), namespace)
    return namespace


class EndpointValidationTests(unittest.TestCase):
    def test_rfc1918_addresses_and_port_defaults(self):
        for ip in ("10.1.2.3", "172.16.0.1", "172.31.255.254",
                   "192.168.0.2", "192.168.255.250"):
            with self.subTest(ip=ip):
                self.assertEqual(validate_device_endpoint(ip, None), (ip, 80))
                self.assertEqual(validate_device_endpoint(ip, "8080"), (ip, 8080))
        self.assertEqual(validate_device_endpoint(" 192.168.1.7 ", 443),
                         ("192.168.1.7", 443))

    def test_rejects_non_lan_and_url_injection(self):
        invalid = ("", "localhost", "8.8.8.8", "127.0.0.1",
                   "169.254.1.1", "100.64.0.1", "0.0.0.0",
                   "255.255.255.255", "224.0.0.1", "192.0.2.1",
                   "172.15.1.2", "172.32.1.2", "192.168.256.1",
                   "192.168.1.1@evil", "192.168.1.1:8080",
                   "192.168.1.1/secret", "192.168.001.001",
                   "::1", "::ffff:192.168.1.2", None, 123)
        for ip in invalid:
            with self.subTest(ip=ip), self.assertRaises(ValueError):
                validate_device_endpoint(ip)

    def test_rejects_bad_ports(self):
        for port in (0, -1, 65536, True, False, 80.0, [],
                     "0", "65536", "-1", "80/path", "80?x=1",
                     " 80", "80 ", "８０", "1e2"):
            with self.subTest(port=port), self.assertRaises(ValueError):
                validate_device_endpoint("192.168.1.3", port)

    def test_disk_endpoint_is_validated_before_proxy(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "endpoint.json"
            ns = {"DEVICE_ENDPOINT_FILE": path, "json": json,
                  "validate_device_endpoint": validate_device_endpoint}
            load_server_functions({"_load_device_endpoint", "_device_base_url"}, ns)
            path.write_text(json.dumps({"ip": "192.168.1.8", "port": "8080"}))
            self.assertEqual(ns["_device_base_url"](), "http://192.168.1.8:8080")
            for bad in ({"ip": "127.0.0.1", "port": 80},
                        {"ip": "192.168.1.8", "port": 65536},
                        {"ip": "192.168.1.8/evil", "port": 80},
                        {"ip": "8.8.8.8", "port": 80}, {"port": 80},
                        ["invalid"], "bad"):
                with self.subTest(bad=bad):
                    path.write_text(json.dumps(bad))
                    self.assertEqual(ns["_load_device_endpoint"](), {})
                    self.assertIsNone(ns["_device_base_url"]())

    def test_registration_and_manual_endpoint_reject_before_write(self):
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp) / "device"
            endpoint = directory / "endpoint.json"
            ns = {
                "DEVICE_LOG_DIR": directory, "DEVICE_ENDPOINT_FILE": endpoint,
                "DOWNLOAD_KEYS": ("test-key",), "authorized_download_key": authorized_download_key,
                "request": request,
                "abort": abort, "jsonify": jsonify, "datetime": datetime,
                "json": json, "validate_device_endpoint": validate_device_endpoint,
            }
            load_server_functions({"_load_device_endpoint", "esp_device_register",
                                   "api_device_set_endpoint"}, ns)
            app = Flask(__name__)
            @app.post("/register/<key>")
            def register(key):
                return ns["esp_device_register"](key)
            @app.post("/manual")
            def manual():
                return ns["api_device_set_endpoint"]()
            with app.test_client() as client:
                invalid = client.post("/register/test-key", json={"port": 80},
                                      environ_overrides={"REMOTE_ADDR": "127.0.0.1"})
                self.assertEqual(invalid.status_code, 400)
                self.assertFalse(endpoint.exists())
                self.assertEqual(client.post("/register/bad", json={"port": 80},
                    environ_overrides={"REMOTE_ADDR": "192.168.1.8"}).status_code, 404)
                self.assertFalse(endpoint.exists())
                self.assertEqual(client.post("/register/test-key", json={"port": "65536"},
                    environ_overrides={"REMOTE_ADDR": "192.168.1.8"}).status_code, 400)
                self.assertFalse(endpoint.exists())
                good = client.post("/register/test-key", json={"port": 8080},
                    environ_overrides={"REMOTE_ADDR": "192.168.1.8"})
                self.assertEqual(good.status_code, 200)
                self.assertEqual(json.loads(endpoint.read_text())["ip"], "192.168.1.8")
                baseline = endpoint.read_bytes()
                self.assertEqual(client.post("/manual",
                    data={"ip": "127.0.0.1", "port": "80"}).status_code, 400)
                self.assertEqual(client.post("/manual",
                    data={"ip": "192.168.1.8", "port": "99999"}).status_code, 400)
                self.assertEqual(endpoint.read_bytes(), baseline)
                self.assertEqual(client.post("/manual",
                    data={"ip": "10.0.0.12", "port": "80"}).status_code, 200)
                self.assertEqual(json.loads(endpoint.read_text())["ip"], "10.0.0.12")

    def test_device_egress_ignores_proxies_and_redirects(self):
        class Session:
            def __init__(self):
                self.trust_env = True
            def __enter__(self):
                return self
            def __exit__(self, *args):
                return None
            def request(self, method, url, **kwargs):
                self.call = (method, url, kwargs)
                self.checked_trust_env = self.trust_env
                return SimpleNamespace(status_code=302, text="redirect")
        instance = Session()
        ns = {"requests": SimpleNamespace(Session=lambda: instance)}
        load_server_functions({"_device_request"}, ns)
        response = ns["_device_request"]("GET", "http://192.168.1.8:80/ping",
                                         timeout=4)
        self.assertEqual(response.status_code, 302)
        self.assertFalse(instance.checked_trust_env)
        self.assertFalse(instance.call[2]["allow_redirects"])
        self.assertEqual(instance.call[2]["timeout"], 4)

    def test_both_device_egress_paths_use_hardened_request(self):
        tree = ast.parse(SERVER.read_text(encoding="utf-8"))
        for name in ("_lan_push_now", "_device_http"):
            fn = next(n for n in tree.body if isinstance(n, ast.FunctionDef)
                      and n.name == name)
            calls = [n.func.id for n in ast.walk(fn)
                     if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)]
            self.assertIn("_device_request", calls)
            self.assertNotIn("get", calls)
            self.assertNotIn("post", calls)


if __name__ == "__main__":
    unittest.main()
