import hashlib
import importlib.util
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

SPEC = importlib.util.spec_from_file_location(
    "download_requirements", Path(__file__).with_name("download_requirements.py")
)
download = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(download)


class EvidenceSnapshotTests(unittest.TestCase):
    def setUp(self):
        self.content = b"<html>Reviewed source</html>"
        self.source = {
            "id": "example",
            "url": "https://example.test/requirements",
            "retrieved_at": "2026-11-01T10:00:00Z",
            "published_at": None,
            "reviewed_sha256": hashlib.sha256(self.content).hexdigest(),
        }

    def test_cached_evidence_retains_actual_retrieval_date(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory)
            (output / "example.html").write_bytes(self.content)
            result = download.acquire_source(self.source, output, False)
            self.assertTrue(result["matches_reviewed_snapshot"])
            self.assertEqual(result["retrieved_at"], "2026-11-01T10:00:00Z")
            self.assertIsNone(result["published_at"])

    def test_corrupted_cache_is_not_treated_as_reviewed_evidence(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory)
            (output / "example.html").write_bytes(b"corrupted")
            with self.assertRaisesRegex(ValueError, "checksum differs"):
                download.acquire_source(self.source, output, False)

    def test_refetch_does_not_overwrite_reviewed_snapshot_or_claim_dates(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory)
            cached = output / "example.html"
            cached.write_bytes(self.content)
            with patch.object(download, "urlopen") as urlopen:
                response = urlopen.return_value.__enter__.return_value
                response.read.return_value = b"<html>New version</html>"
                response.url = self.source["url"]
                response.headers.get_content_type.return_value = "text/html"
                result = download.acquire_source(self.source, output, True)
            self.assertFalse(result["matches_reviewed_snapshot"])
            self.assertEqual(cached.read_bytes(), self.content)
            self.assertEqual(result["reviewed_retrieved_at"], self.source["retrieved_at"])
            self.assertEqual(self.source["retrieved_at"], "2026-11-01T10:00:00Z")
            self.assertEqual(len(list(output.glob("*.html"))), 2)


if __name__ == "__main__":
    unittest.main()
