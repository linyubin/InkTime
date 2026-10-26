"""R2 batch validation and atomic pointer publication contract tests."""
import hashlib
import json
from pathlib import Path
import tempfile
import unittest

from batch_storage import BatchValidationError, publish_batch, read_current


def create_artifacts(root: Path, *, orientation="portrait", bad_palette=False) -> None:
    w, h = (480, 800) if orientation == "portrait" else (800, 480)
    root.mkdir(parents=True, exist_ok=True)
    data = bytearray([1]) * (w * h)
    if bad_palette:
        data[3] = 255
    (root / "photo_0.bin").write_bytes(data)
    (root / "photo_0.json").write_text(
        json.dumps({"orientation": orientation, "w": w, "h": h}), encoding="utf-8"
    )


class BatchStorageTests(unittest.TestCase):
    def test_publish_portrait_and_read_manifest(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source = root / "source"
            create_artifacts(source)
            manifest = publish_batch(root / "delivery", source, [0])
            self.assertEqual(read_current(root / "delivery"), manifest)
            photo = manifest["photos"][0]
            self.assertEqual(photo["bytes"], 384000)
            self.assertEqual(photo["sha256"], hashlib.sha256(bytes([1]) * 384000).hexdigest())
            self.assertTrue((root / "delivery" / "batches" / manifest["batch_id"] / "photo_0.bin").exists())

    def test_landscape_uses_identical_byte_count(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            create_artifacts(root / "source", orientation="landscape")
            photo = publish_batch(root / "delivery", root / "source", [0])["photos"][0]
            self.assertEqual((photo["width"], photo["height"], photo["bytes"]), (800, 480, 384000))

    def test_failure_does_not_replace_current(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source = root / "source"
            create_artifacts(source)
            first = publish_batch(root / "delivery", source, [0])
            create_artifacts(source, bad_palette=True)
            with self.assertRaises(BatchValidationError):
                publish_batch(root / "delivery", source, [0])
            self.assertEqual(read_current(root / "delivery")["batch_id"], first["batch_id"])

    def test_reject_sidecar_dimension_mismatch(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source = root / "source"
            create_artifacts(source)
            (source / "photo_0.json").write_text(
                json.dumps({"orientation": "portrait", "w": 800, "h": 480})
            )
            with self.assertRaises(BatchValidationError):
                publish_batch(root / "delivery", source, [0])
            self.assertFalse((root / "delivery" / "current.json").exists())

    def test_read_current_rejects_pointer_traversal(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / "current.json").write_text(json.dumps({"batch_id": "../../etc"}))
            with self.assertRaises(BatchValidationError):
                read_current(root)


if __name__ == "__main__":
    unittest.main()
