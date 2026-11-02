"""Regression tests for the shell delivery checker using synthetic logs only."""
from pathlib import Path
import subprocess
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "inktime_check.sh"
DAY = "2026-11-02"
ROUND = f"[{DAY} 04:00:00] InkTime 每日编排启动 | BLE=0 ESP32=1"
RENDERED = f"[{DAY} 04:10:00] Step 1: 渲染完成 ✅"
FAILED = f"[{DAY} 04:10:00] Step 1: 渲染失败 ❌"
BLE_OK = f"[{DAY} 04:11:00] Step 2: BLE 推送完成 ✅"


def transfer(at="04:10:01", path="photo_0.bin", status=200, ip="10.0.0.55"):
    return (
        f"{DAY} {at} | {ip} | GET "
        f"/static/inktime/[REDACTED]/{path} | {status} | 384000B | 9ms | ESP32"
    )


def judge(round_log, lines, ble=0, esp=1):
    source = SCRIPT.read_text(encoding="utf-8")
    start = source.index("judge_round() {")
    end = source.index("\n}\n", start) + 2
    function = source[start:end]
    with tempfile.TemporaryDirectory() as temp:
        directory = Path(temp)
        rounds = directory / "render.log"
        downloads = directory / "transfer.log"
        rounds.write_text(round_log, encoding="utf-8")
        downloads.write_text("\n".join(lines) + "\n", encoding="utf-8")
        shell = (
            "set -uo pipefail\n" + function +
            f"\nDO_BLE={ble}\nDO_ESP32={esp}\n"
            'TRANSFER_LOG="$1"\n'
            'judge_round "$(cat "$2")" 2026-11-02\n'
        )
        return subprocess.run(
            ["bash", "-c", shell, "bash", str(downloads), str(rounds)],
            capture_output=True, text=True, check=False,
        )


class DeliveryCheckTests(unittest.TestCase):
    def test_current_round_successful_render_and_device_fetch(self):
        p = judge(ROUND + "\n" + RENDERED, [transfer()])
        self.assertEqual(p.returncode, 0, p.stderr)

    def test_yesterday_round_does_not_count(self):
        p = judge("[2026-11-01 04:00:00] InkTime 每日编排启动\n"
                  "[2026-11-01 04:10:00] Step 1: 渲染完成 ✅", [transfer()])
        self.assertNotEqual(p.returncode, 0)

    def test_render_failure_cannot_be_covered_by_old_photo_download(self):
        p = judge(ROUND + "\n" + FAILED, [transfer(at="04:20:00")])
        self.assertNotEqual(p.returncode, 0)

    def test_fetch_before_render_completion_does_not_count(self):
        p = judge(ROUND + "\n" + RENDERED, [transfer(at="04:05:00")])
        self.assertNotEqual(p.returncode, 0)

    def test_calibration_and_loopback_are_not_device_photo_delivery(self):
        p = judge(ROUND + "\n" + RENDERED,
                  [transfer(path="calib_p.bin"), transfer(ip="127.0.0.1")])
        self.assertNotEqual(p.returncode, 0)

    def test_non_200_is_not_success(self):
        p = judge(ROUND + "\n" + RENDERED, [transfer(status=404)])
        self.assertNotEqual(p.returncode, 0)

    def test_ble_delivery_success(self):
        p = judge(ROUND + "\n" + RENDERED + "\n" + BLE_OK, [], ble=1, esp=0)
        self.assertEqual(p.returncode, 0, p.stderr)


if __name__ == "__main__":
    unittest.main()
