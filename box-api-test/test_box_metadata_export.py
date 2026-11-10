import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock

from box_metadata_export import BoxClient, export_files, parse_query_param


class FakeResponse:
    def __init__(self, payload, status_code=200, headers=None):
        self.payload = payload
        self.status_code = status_code
        self.headers = headers or {}
        self.ok = 200 <= status_code < 300
        self.text = json.dumps(payload)

    def json(self):
        return self.payload


class BoxMetadataExportTests(unittest.TestCase):
    def test_parse_query_param_preserves_json_types(self):
        self.assertEqual(parse_query_param("year=2026"), ("year", 2026))
        self.assertEqual(
            parse_query_param('groups=["legal","finance"]'),
            ("groups", ["legal", "finance"]),
        )
        self.assertEqual(parse_query_param("status=approved"), ("status", "approved"))

    def test_export_follows_markers_and_writes_one_json_per_file(self):
        session = Mock()
        session.headers = {}

        def request(method, url, **kwargs):
            if url.endswith("/metadata_queries/execute_read"):
                if kwargs["json"].get("marker") == "next":
                    return FakeResponse(
                        {
                            "entries": [
                                {"type": "file", "id": "222"},
                                {"type": "folder", "id": "333"},
                            ],
                            "next_marker": None,
                        }
                    )
                return FakeResponse(
                    {
                        "entries": [{"type": "file", "id": "111"}],
                        "next_marker": "next",
                    }
                )
            if url.endswith("/files/111"):
                return FakeResponse(
                    {"type": "file", "id": "111", "name": "a.txt"}
                )
            if url.endswith("/files/222"):
                return FakeResponse(
                    {"type": "file", "id": "222", "name": "b.txt"}
                )
            if url.endswith("/files/111/metadata"):
                return FakeResponse({"entries": [{"$template": "contract"}]})
            if url.endswith("/files/222/metadata"):
                return FakeResponse({"entries": []})
            raise AssertionError(f"Unexpected request: {method} {url}")

        session.request.side_effect = request
        client = BoxClient("test-token", session=session)

        with tempfile.TemporaryDirectory() as temporary_directory:
            output_dir = Path(temporary_directory)
            count = export_files(
                client,
                folder_id="999",
                from_template="enterprise_1.contract",
                query="status = :status",
                query_params={"status": "approved"},
                output_dir=output_dir,
                limit=100,
            )

            self.assertEqual(count, 2)
            self.assertTrue((output_dir / "111.json").is_file())
            self.assertTrue((output_dir / "222.json").is_file())
            self.assertFalse((output_dir / "333.json").exists())
            document = json.loads((output_dir / "111.json").read_text("utf-8"))
            self.assertEqual(document["file"]["name"], "a.txt")
            self.assertEqual(
                document["metadata"]["entries"][0]["$template"], "contract"
            )

        marker_requests = [
            call.kwargs["json"]
            for call in session.request.call_args_list
            if call.args[1].endswith("/metadata_queries/execute_read")
        ]
        self.assertEqual(len(marker_requests), 2)
        self.assertNotIn("marker", marker_requests[0])
        self.assertEqual(marker_requests[1]["marker"], "next")


if __name__ == "__main__":
    unittest.main()
