#!/usr/bin/env python
"""Export Box metadata query matches as one JSON document per file."""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

import requests


BOX_API_BASE_URL = "https://api.box.com/2.0"
DEFAULT_FILE_FIELDS = (
    "id,type,name,description,size,etag,sha1,path_collection,"
    "created_at,modified_at,content_created_at,content_modified_at,"
    "created_by,modified_by,owned_by,parent,item_status,trashed_at,"
    "purged_at,shared_link"
)


class BoxApiError(RuntimeError):
    """Raised when the Box API does not return a successful response."""


class BoxClient:
    def __init__(
        self,
        access_token: str,
        *,
        base_url: str = BOX_API_BASE_URL,
        timeout: float = 30,
        max_retries: int = 5,
        session: requests.Session | None = None,
    ) -> None:
        if not access_token:
            raise ValueError("A Box access token is required")

        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.max_retries = max_retries
        self.session = session or requests.Session()
        self.session.headers.update(
            {
                "Authorization": f"Bearer {access_token}",
                "Accept": "application/json",
                "Content-Type": "application/json",
            }
        )

    def request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        body: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        url = f"{self.base_url}{path}"

        for attempt in range(self.max_retries + 1):
            try:
                response = self.session.request(
                    method,
                    url,
                    params=params,
                    json=body,
                    timeout=self.timeout,
                )
            except requests.RequestException as exc:
                if attempt == self.max_retries:
                    raise BoxApiError(f"{method} {path} failed: {exc}") from exc
                time.sleep(2**attempt)
                continue

            if response.status_code == 429 or response.status_code >= 500:
                if attempt < self.max_retries:
                    retry_after = response.headers.get("Retry-After")
                    delay = (
                        float(retry_after)
                        if retry_after and retry_after.replace(".", "", 1).isdigit()
                        else 2**attempt
                    )
                    time.sleep(delay)
                    continue

            if not response.ok:
                try:
                    detail = response.json()
                except ValueError:
                    detail = response.text
                raise BoxApiError(
                    f"{method} {path} failed with HTTP {response.status_code}: "
                    f"{detail}"
                )

            try:
                return response.json()
            except ValueError as exc:
                raise BoxApiError(
                    f"{method} {path} returned a non-JSON response"
                ) from exc

        raise AssertionError("unreachable")

    def metadata_query(
        self,
        *,
        from_template: str,
        ancestor_folder_id: str,
        query: str,
        query_params: dict[str, Any],
        fields: Iterable[str],
        limit: int,
    ) -> Iterable[dict[str, Any]]:
        marker: str | None = None

        while True:
            body: dict[str, Any] = {
                "from": from_template,
                "ancestor_folder_id": ancestor_folder_id,
                "query": query,
                "query_params": query_params,
                "fields": list(fields),
                "limit": limit,
            }
            if marker:
                body["marker"] = marker

            page = self.request(
                "POST", "/metadata_queries/execute_read", body=body
            )
            entries = page.get("entries")
            if not isinstance(entries, list):
                raise BoxApiError("Metadata query response has no entries array")

            yield from entries

            next_marker = page.get("next_marker")
            if not next_marker:
                break
            if next_marker == marker:
                raise BoxApiError("Metadata query returned a repeated next_marker")
            marker = str(next_marker)

    def get_file(self, file_id: str) -> dict[str, Any]:
        return self.request(
            "GET",
            f"/files/{file_id}",
            params={"fields": DEFAULT_FILE_FIELDS},
        )

    def get_file_metadata(self, file_id: str) -> dict[str, Any]:
        return self.request("GET", f"/files/{file_id}/metadata")


def parse_query_param(value: str) -> tuple[str, Any]:
    if "=" not in value:
        raise argparse.ArgumentTypeError("query params must use NAME=VALUE")
    name, raw_value = value.split("=", 1)
    if not name:
        raise argparse.ArgumentTypeError("query param name cannot be empty")
    try:
        parsed_value = json.loads(raw_value)
    except json.JSONDecodeError:
        parsed_value = raw_value
    return name, parsed_value


def write_json_atomic(path: Path, document: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path = path.with_suffix(f"{path.suffix}.tmp")
    try:
        with temporary_path.open("w", encoding="utf-8") as output:
            json.dump(document, output, ensure_ascii=False, indent=2)
            output.write("\n")
        temporary_path.replace(path)
    finally:
        temporary_path.unlink(missing_ok=True)


def export_files(
    client: BoxClient,
    *,
    folder_id: str,
    from_template: str,
    query: str,
    query_params: dict[str, Any],
    output_dir: Path,
    limit: int,
) -> int:
    matches = client.metadata_query(
        from_template=from_template,
        ancestor_folder_id=folder_id,
        query=query,
        query_params=query_params,
        fields=("id", "type"),
        limit=limit,
    )
    exported_ids: set[str] = set()

    for match in matches:
        if match.get("type") != "file":
            continue
        file_id = str(match.get("id", ""))
        if not file_id:
            raise BoxApiError("Metadata query returned an entry without an id")
        if file_id in exported_ids:
            continue

        document = {
            "exported_at": datetime.now(timezone.utc).isoformat(),
            "source": {
                "ancestor_folder_id": folder_id,
                "from": from_template,
                "query": query,
                "query_params": query_params,
            },
            "query_result": match,
            "file": client.get_file(file_id),
            "metadata": client.get_file_metadata(file_id),
        }
        write_json_atomic(output_dir / f"{file_id}.json", document)
        exported_ids.add(file_id)

    return len(exported_ids)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Find files below a Box folder with the Metadata Query API and "
            "export one JSON file per matched file."
        )
    )
    parser.add_argument(
        "--folder-id",
        required=True,
        help="Ancestor Box folder ID. Use 0 for the app user's root folder.",
    )
    parser.add_argument(
        "--from-template",
        required=True,
        help="Metadata template source, for example enterprise_123456.myTemplate.",
    )
    parser.add_argument(
        "--query",
        required=True,
        help="Metadata query predicate, for example status = :status.",
    )
    parser.add_argument(
        "--param",
        action="append",
        default=[],
        metavar="NAME=VALUE",
        type=parse_query_param,
        help=(
            "Query parameter. Repeat for multiple values; VALUE is parsed as "
            "JSON when possible."
        ),
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=Path("metadata-export"),
        help="Output directory (default: metadata-export).",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=100,
        help="Metadata query page size from 1 to 100 (default: 100).",
    )
    parser.add_argument(
        "--access-token",
        default=os.environ.get("BOX_ACCESS_TOKEN"),
        help="Box access token. Prefer the BOX_ACCESS_TOKEN environment variable.",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=30,
        help="Box API request timeout in seconds (default: 30).",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    if not 1 <= args.limit <= 100:
        parser.error("--limit must be from 1 to 100")
    if not args.access_token:
        parser.error(
            "--access-token or the BOX_ACCESS_TOKEN environment variable is required"
        )

    try:
        query_params = dict(args.param)
        client = BoxClient(args.access_token, timeout=args.timeout)
        count = export_files(
            client,
            folder_id=args.folder_id,
            from_template=args.from_template,
            query=args.query,
            query_params=query_params,
            output_dir=args.output_dir,
            limit=args.limit,
        )
    except (BoxApiError, ValueError, OSError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1

    print(f"Exported {count} file(s) to {args.output_dir.resolve()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
