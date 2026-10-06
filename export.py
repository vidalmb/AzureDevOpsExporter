from __future__ import annotations

import argparse
import base64
import json
import logging
import os
import sqlite3
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Iterator
from urllib.parse import quote, urlparse

import requests
import yaml


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def chunks(values: list[int], size: int) -> Iterator[list[int]]:
    for index in range(0, len(values), size):
        yield values[index:index + size]


def json_text(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def append_jsonl(path: Path, record: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json_text(record))
        handle.write("\n")


def overwrite_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(value, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    temporary.replace(path)


@dataclass
class ExportManifest:
    organization_url: str
    project: str
    started_utc: str = field(default_factory=utc_now)
    finished_utc: str | None = None
    status: str = "running"
    counts: dict[str, int] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)
    errors: list[dict[str, Any]] = field(default_factory=list)

    def increment(self, name: str, amount: int = 1) -> None:
        self.counts[name] = self.counts.get(name, 0) + amount

    def as_dict(self) -> dict[str, Any]:
        return {
            "organization_url": self.organization_url,
            "project": self.project,
            "started_utc": self.started_utc,
            "finished_utc": self.finished_utc,
            "status": self.status,
            "counts": self.counts,
            "warnings": self.warnings,
            "errors": self.errors,
        }


class AzureDevOpsError(RuntimeError):
    pass


class AzureDevOpsClient:
    def __init__(
        self,
        organization_url: str,
        pat: str,
        api_version: str,
        timeout: int,
        max_retries: int,
        retry_base_seconds: float,
    ) -> None:
        self.organization_url = organization_url.rstrip("/")
        self.api_version = api_version
        self.timeout = timeout
        self.max_retries = max_retries
        self.retry_base_seconds = retry_base_seconds

        auth_bytes = base64.b64encode(f":{pat}".encode("utf-8")).decode("ascii")
        self.session = requests.Session()
        self.session.headers.update(
            {
                "Authorization": f"Basic {auth_bytes}",
                "Accept": "application/json",
                "User-Agent": "AzureDevOps-Exporter/1.0",
            }
        )

    def url(self, path: str) -> str:
        if path.startswith("http://") or path.startswith("https://"):
            return path
        return f"{self.organization_url}/{path.lstrip('/')}"

    def request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        json_body: Any | None = None,
    ) -> requests.Response:
        request_params = dict(params or {})
        request_params.setdefault("api-version", self.api_version)
        url = self.url(path)

        for attempt in range(self.max_retries + 1):
            response = self.session.request(
                method,
                url,
                params=request_params,
                json=json_body,
                timeout=self.timeout,
            )

            if response.status_code < 400:
                return response

            retryable = response.status_code == 429 or 500 <= response.status_code < 600
            if retryable and attempt < self.max_retries:
                retry_after = response.headers.get("Retry-After")
                try:
                    wait_seconds = float(retry_after) if retry_after else (
                        self.retry_base_seconds * (2 ** attempt)
                    )
                except ValueError:
                    wait_seconds = self.retry_base_seconds * (2 ** attempt)

                logging.warning(
                    "HTTP %s en %s. Reintento %s/%s en %.1f segundos.",
                    response.status_code,
                    response.url,
                    attempt + 1,
                    self.max_retries,
                    wait_seconds,
                )
                time.sleep(wait_seconds)
                continue

            body = response.text[:2000]
            raise AzureDevOpsError(
                f"HTTP {response.status_code} al llamar a {response.url}\n{body}"
            )

        raise AzureDevOpsError(f"No se pudo completar la llamada a {url}")

    def get_json(
        self,
        path: str,
        *,
        params: dict[str, Any] | None = None,
    ) -> tuple[Any, requests.Response]:
        response = self.request("GET", path, params=params)
        if not response.content:
            return {}, response
        return response.json(), response

    def post_json(
        self,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        json_body: Any,
    ) -> tuple[Any, requests.Response]:
        response = self.request("POST", path, params=params, json_body=json_body)
        if not response.content:
            return {}, response
        return response.json(), response

    def list_by_continuation_token(
        self,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        value_key: str = "value",
    ) -> Iterator[Any]:
        token: str | None = None

        while True:
            current_params = dict(params or {})
            if token:
                current_params["continuationToken"] = token

            payload, response = self.get_json(path, params=current_params)
            records = payload.get(value_key, []) if isinstance(payload, dict) else []
            for record in records:
                yield record

            token = (
                response.headers.get("x-ms-continuationtoken")
                or response.headers.get("X-MS-ContinuationToken")
            )
            if not token:
                break


class Database:
    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self.connection = sqlite3.connect(path)
        self.connection.execute("PRAGMA journal_mode=WAL")
        self.connection.execute("PRAGMA foreign_keys=ON")
        self.create_schema()

    def create_schema(self) -> None:
        self.connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS project (
                id TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                state TEXT,
                visibility TEXT,
                raw_json TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS teams (
                id TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                description TEXT,
                raw_json TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS work_items (
                id INTEGER PRIMARY KEY,
                rev INTEGER,
                url TEXT,
                fields_json TEXT NOT NULL,
                relations_json TEXT,
                raw_json TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS revisions (
                work_item_id INTEGER NOT NULL,
                rev INTEGER NOT NULL,
                raw_json TEXT NOT NULL,
                PRIMARY KEY (work_item_id, rev)
            );

            CREATE TABLE IF NOT EXISTS updates (
                work_item_id INTEGER NOT NULL,
                update_id INTEGER NOT NULL,
                raw_json TEXT NOT NULL,
                PRIMARY KEY (work_item_id, update_id)
            );

            CREATE TABLE IF NOT EXISTS comments (
                work_item_id INTEGER NOT NULL,
                comment_id INTEGER NOT NULL,
                version INTEGER,
                created_by TEXT,
                created_date TEXT,
                modified_date TEXT,
                text TEXT,
                raw_json TEXT NOT NULL,
                PRIMARY KEY (work_item_id, comment_id)
            );

            CREATE TABLE IF NOT EXISTS team_area_paths (
                team_id TEXT NOT NULL,
                team_name TEXT NOT NULL,
                area_path TEXT NOT NULL,
                include_children INTEGER NOT NULL DEFAULT 0,
                is_default INTEGER NOT NULL DEFAULT 0,
                raw_json TEXT NOT NULL,
                PRIMARY KEY (team_id, area_path)
            );

            CREATE TABLE IF NOT EXISTS metadata (
                category TEXT NOT NULL,
                item_key TEXT NOT NULL,
                raw_json TEXT NOT NULL,
                PRIMARY KEY (category, item_key)
            );

            CREATE INDEX IF NOT EXISTS idx_comments_work_item
            ON comments(work_item_id);

            CREATE INDEX IF NOT EXISTS idx_revisions_work_item
            ON revisions(work_item_id);

            CREATE INDEX IF NOT EXISTS idx_updates_work_item
            ON updates(work_item_id);

            CREATE INDEX IF NOT EXISTS idx_team_area_paths_team_name
            ON team_area_paths(team_name);

            CREATE INDEX IF NOT EXISTS idx_team_area_paths_area_path
            ON team_area_paths(area_path);
            """
        )
        self.connection.commit()

    def upsert_project(self, record: dict[str, Any]) -> None:
        self.connection.execute(
            """
            INSERT INTO project(id, name, state, visibility, raw_json)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                name=excluded.name,
                state=excluded.state,
                visibility=excluded.visibility,
                raw_json=excluded.raw_json
            """,
            (
                record.get("id"),
                record.get("name"),
                record.get("state"),
                record.get("visibility"),
                json_text(record),
            ),
        )

    def upsert_team(self, record: dict[str, Any]) -> None:
        self.connection.execute(
            """
            INSERT INTO teams(id, name, description, raw_json)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                name=excluded.name,
                description=excluded.description,
                raw_json=excluded.raw_json
            """,
            (
                record.get("id"),
                record.get("name"),
                record.get("description"),
                json_text(record),
            ),
        )

    def upsert_work_item(self, record: dict[str, Any]) -> None:
        self.connection.execute(
            """
            INSERT INTO work_items(
                id, rev, url, fields_json, relations_json, raw_json
            )
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                rev=excluded.rev,
                url=excluded.url,
                fields_json=excluded.fields_json,
                relations_json=excluded.relations_json,
                raw_json=excluded.raw_json
            """,
            (
                record["id"],
                record.get("rev"),
                record.get("url"),
                json_text(record.get("fields", {})),
                json_text(record.get("relations", [])),
                json_text(record),
            ),
        )

    def upsert_revision(self, work_item_id: int, record: dict[str, Any]) -> None:
        self.connection.execute(
            """
            INSERT INTO revisions(work_item_id, rev, raw_json)
            VALUES (?, ?, ?)
            ON CONFLICT(work_item_id, rev) DO UPDATE SET
                raw_json=excluded.raw_json
            """,
            (work_item_id, record.get("rev", 0), json_text(record)),
        )

    def upsert_update(self, work_item_id: int, record: dict[str, Any]) -> None:
        update_id = record.get("id", record.get("rev", 0))
        self.connection.execute(
            """
            INSERT INTO updates(work_item_id, update_id, raw_json)
            VALUES (?, ?, ?)
            ON CONFLICT(work_item_id, update_id) DO UPDATE SET
                raw_json=excluded.raw_json
            """,
            (work_item_id, update_id, json_text(record)),
        )

    def upsert_comment(self, work_item_id: int, record: dict[str, Any]) -> None:
        created_by = record.get("createdBy")
        if isinstance(created_by, dict):
            created_by = created_by.get("displayName") or created_by.get("uniqueName")

        self.connection.execute(
            """
            INSERT INTO comments(
                work_item_id, comment_id, version, created_by,
                created_date, modified_date, text, raw_json
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(work_item_id, comment_id) DO UPDATE SET
                version=excluded.version,
                created_by=excluded.created_by,
                created_date=excluded.created_date,
                modified_date=excluded.modified_date,
                text=excluded.text,
                raw_json=excluded.raw_json
            """,
            (
                work_item_id,
                record.get("id"),
                record.get("version"),
                created_by,
                record.get("createdDate"),
                record.get("modifiedDate"),
                record.get("text"),
                json_text(record),
            ),
        )

    def replace_team_area_paths(
        self,
        team_id: str,
        team_name: str,
        default_value: str | None,
        values: list[dict[str, Any]],
    ) -> None:
        self.connection.execute(
            "DELETE FROM team_area_paths WHERE team_id = ?",
            (team_id,),
        )

        normalized_default = (default_value or "").casefold()

        for record in values:
            area_path = record.get("value")
            if not area_path:
                continue

            self.connection.execute(
                """
                INSERT INTO team_area_paths(
                    team_id, team_name, area_path, include_children,
                    is_default, raw_json
                )
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(team_id, area_path) DO UPDATE SET
                    team_name=excluded.team_name,
                    include_children=excluded.include_children,
                    is_default=excluded.is_default,
                    raw_json=excluded.raw_json
                """,
                (
                    team_id,
                    team_name,
                    area_path,
                    1 if bool(record.get("includeChildren")) else 0,
                    1 if area_path.casefold() == normalized_default else 0,
                    json_text(record),
                ),
            )

    def upsert_metadata(
        self,
        category: str,
        item_key: str,
        record: dict[str, Any],
    ) -> None:
        self.connection.execute(
            """
            INSERT INTO metadata(category, item_key, raw_json)
            VALUES (?, ?, ?)
            ON CONFLICT(category, item_key) DO UPDATE SET
                raw_json=excluded.raw_json
            """,
            (category, item_key, json_text(record)),
        )

    def commit(self) -> None:
        self.connection.commit()

    def close(self) -> None:
        self.connection.commit()
        self.connection.close()


class Exporter:
    def __init__(self, config: dict[str, Any], pat: str) -> None:
        self.config = config
        self.organization_url = config["organization_url"].rstrip("/")
        self.project = config["project"]
        self.encoded_project = quote(self.project, safe="")
        self.output = Path(config.get("output_directory", "output")).resolve()
        self.raw = self.output / "raw"
        self.logs = self.output / "logs"
        self.manifest_path = self.output / "manifest.json"
        self.checkpoint_path = Path("checkpoints.json").resolve()

        self.output.mkdir(parents=True, exist_ok=True)
        self.raw.mkdir(parents=True, exist_ok=True)
        self.logs.mkdir(parents=True, exist_ok=True)

        self.manifest = ExportManifest(
            organization_url=self.organization_url,
            project=self.project,
        )
        self.client = AzureDevOpsClient(
            organization_url=self.organization_url,
            pat=pat,
            api_version=str(config.get("api_version", "7.1")),
            timeout=int(config.get("request_timeout_seconds", 60)),
            max_retries=int(config.get("max_retries", 6)),
            retry_base_seconds=float(config.get("retry_base_seconds", 2)),
        )
        self.database = Database(
            self.output / "database" / "azuredevops.db"
        )
        self.options = config.get("export", {})
        self.page_size = int(config.get("page_size", 200))
        self.batch_size = min(
            200,
            int(config.get("work_item_batch_size", 200)),
        )

    def enabled(self, name: str) -> bool:
        return bool(self.options.get(name, True))

    def write_manifest(self) -> None:
        overwrite_json(self.manifest_path, self.manifest.as_dict())

    def record_error(self, component: str, error: Exception) -> None:
        details = {
            "component": component,
            "message": str(error),
            "utc": utc_now(),
        }
        self.manifest.errors.append(details)
        append_jsonl(self.logs / "errors.jsonl", details)
        logging.exception("Error en %s", component)
        self.write_manifest()

    def export_single_metadata(
        self,
        category: str,
        path: str,
        raw_filename: str,
        *,
        params: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        payload, _ = self.client.get_json(path, params=params)
        append_jsonl(self.raw / raw_filename, payload)
        key = str(payload.get("id") or payload.get("name") or category)
        self.database.upsert_metadata(category, key, payload)
        self.database.commit()
        self.manifest.increment(category)
        return payload

    def export_list_metadata(
        self,
        category: str,
        path: str,
        raw_filename: str,
        *,
        params: dict[str, Any] | None = None,
        use_continuation: bool = False,
    ) -> list[dict[str, Any]]:
        records: list[dict[str, Any]] = []

        if use_continuation:
            iterator = self.client.list_by_continuation_token(path, params=params)
            records.extend(iterator)
        else:
            payload, _ = self.client.get_json(path, params=params)
            if isinstance(payload, dict):
                records.extend(payload.get("value", []))
            elif isinstance(payload, list):
                records.extend(payload)

        for index, record in enumerate(records):
            append_jsonl(self.raw / raw_filename, record)
            key = str(
                record.get("id")
                or record.get("identifier")
                or record.get("referenceName")
                or record.get("name")
                or index
            )
            self.database.upsert_metadata(category, key, record)

        self.database.commit()
        self.manifest.increment(category, len(records))
        return records

    def test_access(self) -> None:
        logging.info("Comprobando acceso a la organización y al proyecto.")
        projects = list(
            self.client.list_by_continuation_token(
                "_apis/projects",
                params={"$top": 100},
            )
        )
        matching = [
            item for item in projects
            if item.get("name", "").casefold() == self.project.casefold()
        ]

        if not matching:
            visible = ", ".join(item.get("name", "?") for item in projects)
            raise AzureDevOpsError(
                f"El proyecto '{self.project}' no aparece entre los proyectos "
                f"visibles para el PAT. Proyectos visibles: {visible or 'ninguno'}"
            )

        payload, _ = self.client.get_json(
            f"{self.encoded_project}/_apis/wit/workitemtypes"
        )
        count = len(payload.get("value", []))
        logging.info(
            "Acceso correcto. Proyecto encontrado y %s tipos de work item visibles.",
            count,
        )
        print(
            f"OK: acceso correcto a '{self.project}'. "
            f"Tipos de work item visibles: {count}."
        )

    def export_project(self) -> dict[str, Any]:
        logging.info("Exportando proyecto.")
        payload, _ = self.client.get_json(
            f"_apis/projects/{self.encoded_project}",
            params={"includeCapabilities": "true"},
        )
        append_jsonl(self.raw / "project.jsonl", payload)
        self.database.upsert_project(payload)
        self.database.commit()
        self.manifest.increment("project")
        return payload

    def export_teams(self) -> list[dict[str, Any]]:
        logging.info("Exportando equipos.")
        records = self.export_list_metadata(
            "teams",
            f"_apis/projects/{self.encoded_project}/teams",
            "teams.jsonl",
            params={"$top": 100},
            use_continuation=True,
        )
        for record in records:
            self.database.upsert_team(record)
        self.database.commit()
        return records

    def export_team_area_paths(
        self,
        teams: list[dict[str, Any]],
    ) -> dict[str, Any]:
        logging.info("Exportando Area Paths configuradas por equipo.")

        export_payload: dict[str, Any] = {
            "project": self.project,
            "exportedUtc": utc_now(),
            "teams": {},
        }

        for team in teams:
            team_id = team.get("id")
            team_name = team.get("name", team_id)
            if not team_id or not team_name:
                continue

            payload, _ = self.client.get_json(
                f"{self.encoded_project}/{quote(team_id, safe='')}/"
                "_apis/work/teamsettings/teamfieldvalues",
                params={"api-version": "7.1-preview.1"},
            )

            values = payload.get("values", [])
            default_value = payload.get("defaultValue")

            team_record = {
                "teamId": team_id,
                "teamName": team_name,
                "field": payload.get("field"),
                "defaultValue": default_value,
                "values": values,
            }
            export_payload["teams"][team_name] = team_record

            self.database.replace_team_area_paths(
                team_id=team_id,
                team_name=team_name,
                default_value=default_value,
                values=values,
            )
            self.database.upsert_metadata(
                "team_area_paths",
                team_id,
                team_record,
            )

            self.manifest.increment("team_area_path_teams")
            self.manifest.increment("team_area_paths", len(values))

        overwrite_json(
            self.raw / "team_area_paths.json",
            export_payload,
        )
        self.database.commit()
        return export_payload

    def export_classification_nodes(self) -> None:
        logging.info("Exportando áreas e iteraciones.")
        for structure in ("areas", "iterations"):
            payload, _ = self.client.get_json(
                f"{self.encoded_project}/_apis/wit/classificationnodes/{structure}",
                params={"$depth": 100},
            )
            append_jsonl(
                self.raw / f"classification_{structure}.jsonl",
                payload,
            )
            self.database.upsert_metadata(
                f"classification_{structure}",
                str(payload.get("id", structure)),
                payload,
            )
            self.manifest.increment(f"classification_{structure}")
        self.database.commit()

    def export_team_iterations(self, teams: list[dict[str, Any]]) -> None:
        logging.info("Exportando iteraciones configuradas por equipo.")
        for team in teams:
            team_id = team.get("id")
            team_name = team.get("name", team_id)
            if not team_id:
                continue
            payload, _ = self.client.get_json(
                f"{self.encoded_project}/{quote(team_id, safe='')}/"
                "_apis/work/teamsettings/iterations"
            )
            for record in payload.get("value", []):
                enriched = {
                    "teamId": team_id,
                    "teamName": team_name,
                    **record,
                }
                append_jsonl(self.raw / "team_iterations.jsonl", enriched)
                key = f"{team_id}:{record.get('id', record.get('name', 'unknown'))}"
                self.database.upsert_metadata("team_iterations", key, enriched)
                self.manifest.increment("team_iterations")
        self.database.commit()

    def export_work_item_types(self) -> None:
        logging.info("Exportando tipos de work item.")
        self.export_list_metadata(
            "work_item_types",
            f"{self.encoded_project}/_apis/wit/workitemtypes",
            "work_item_types.jsonl",
        )

    def export_fields(self) -> None:
        logging.info("Exportando campos.")
        self.export_list_metadata(
            "fields",
            "_apis/wit/fields",
            "fields.jsonl",
        )

    def get_work_item_ids(self) -> list[int]:
        logging.info("Consultando identificadores de work items mediante WIQL.")
        wiql = {
            "query": (
                "SELECT [System.Id] "
                "FROM WorkItems "
                f"WHERE [System.TeamProject] = '{self.project.replace(chr(39), chr(39) * 2)}' "
                "ORDER BY [System.Id]"
            )
        }
        payload, _ = self.client.post_json(
            f"{self.encoded_project}/_apis/wit/wiql",
            params={"$top": 20000},
            json_body=wiql,
        )
        ids = [
            int(item["id"])
            for item in payload.get("workItems", [])
            if item.get("id") is not None
        ]
        if len(ids) == 20000:
            warning = (
                "WIQL devolvió exactamente 20.000 work items. "
                "Puede existir un límite de resultados; conviene revisar si el "
                "proyecto contiene más elementos."
            )
            self.manifest.warnings.append(warning)
            logging.warning(warning)

        self.manifest.counts["work_item_ids"] = len(ids)
        return ids

    def export_work_items(self, ids: list[int]) -> None:
        logging.info("Exportando %s work items.", len(ids))
        for batch_number, batch in enumerate(chunks(ids, self.batch_size), start=1):
            body = {
                "ids": batch,
                "$expand": "All",
                "errorPolicy": "Omit",
            }
            payload, _ = self.client.post_json(
                f"{self.encoded_project}/_apis/wit/workitemsbatch",
                json_body=body,
            )
            records = payload.get("value", [])
            for record in records:
                append_jsonl(self.raw / "work_items.jsonl", record)
                self.database.upsert_work_item(record)
                self.manifest.increment("work_items")
            self.database.commit()
            logging.info(
                "Lote %s: %s work items exportados.",
                batch_number,
                len(records),
            )
            self.write_manifest()

    def export_revisions_for_item(self, work_item_id: int) -> None:
        skip = 0
        while True:
            payload, _ = self.client.get_json(
                f"{self.encoded_project}/_apis/wit/workItems/"
                f"{work_item_id}/revisions",
                params={"$top": self.page_size, "$skip": skip},
            )
            records = payload.get("value", [])
            for record in records:
                enriched = {"workItemId": work_item_id, **record}
                append_jsonl(self.raw / "revisions.jsonl", enriched)
                self.database.upsert_revision(work_item_id, record)
                self.manifest.increment("revisions")
            if len(records) < self.page_size:
                break
            skip += len(records)

    def export_updates_for_item(self, work_item_id: int) -> None:
        skip = 0
        while True:
            payload, _ = self.client.get_json(
                f"{self.encoded_project}/_apis/wit/workItems/"
                f"{work_item_id}/updates",
                params={"$top": self.page_size, "$skip": skip},
            )
            records = payload.get("value", [])
            for record in records:
                enriched = {"workItemId": work_item_id, **record}
                append_jsonl(self.raw / "updates.jsonl", enriched)
                self.database.upsert_update(work_item_id, record)
                self.manifest.increment("updates")
            if len(records) < self.page_size:
                break
            skip += len(records)

    def export_comments_for_item(self, work_item_id: int) -> None:
        token: str | None = None
        while True:
            params: dict[str, Any] = {
                #"$top": self.page_size,
                #"includeDeleted": "true",
                #"order": "asc",
                "$top": self.page_size,
                "includeDeleted": "true",
                "order": "asc",
                "api-version": "7.1-preview.4",
            }
            if token:
                params["continuationToken"] = token

            payload, response = self.client.get_json(
                f"{self.encoded_project}/_apis/wit/workItems/"
                f"{work_item_id}/comments",
                params=params,
            )
            records = payload.get("comments", payload.get("value", []))
            for record in records:
                enriched = {"workItemId": work_item_id, **record}
                append_jsonl(self.raw / "comments.jsonl", enriched)
                self.database.upsert_comment(work_item_id, record)
                self.manifest.increment("comments")

            token = (
                response.headers.get("x-ms-continuationtoken")
                or payload.get("continuationToken")
                or payload.get("nextPage")
            )
            if not token:
                break

    def export_work_item_history(self, ids: list[int]) -> None:
        logging.info(
            "Exportando revisiones, actualizaciones y comentarios de %s elementos.",
            len(ids),
        )
        for index, work_item_id in enumerate(ids, start=1):
            try:
                if self.enabled("revisions"):
                    self.export_revisions_for_item(work_item_id)
                if self.enabled("updates"):
                    self.export_updates_for_item(work_item_id)
                if self.enabled("comments"):
                    self.export_comments_for_item(work_item_id)
                self.database.commit()
            except Exception as error:
                self.record_error(f"work_item_history:{work_item_id}", error)

            if index % 25 == 0 or index == len(ids):
                logging.info(
                    "Historial procesado: %s/%s work items.",
                    index,
                    len(ids),
                )
                self.write_manifest()

    def export_queries(self) -> None:
        logging.info("Exportando árbol de consultas compartidas.")
        payload, _ = self.client.get_json(
            f"{self.encoded_project}/_apis/wit/queries",
            #params={"$depth": 100, "$expand": "all"},
            params={"$depth": 2, "$expand": "all"},
        )
        append_jsonl(self.raw / "queries.jsonl", payload)
        self.database.upsert_metadata("queries", "root", payload)
        self.database.commit()
        self.manifest.increment("queries")

    def export_dashboards(self, teams: list[dict[str, Any]]) -> None:
        logging.info("Exportando dashboards por equipo.")
        
        dashboard_params = {
        "api-version": "7.1-preview.3",
        }
        
        for team in teams:
            team_id = team.get("id")
            if not team_id:
                continue
            payload, _ = self.client.get_json(
            f"{self.encoded_project}/{quote(team_id, safe='')}/"
            "_apis/dashboard/dashboards",
            params=dashboard_params,
            )
            for dashboard in payload.get("value", []):
                dashboard_id = dashboard.get("id")
                detail = dashboard
                if dashboard_id:
                    detail, _ = self.client.get_json(
                        f"{self.encoded_project}/{quote(team_id, safe='')}/"
                        f"_apis/dashboard/dashboards/"
                        f"{quote(dashboard_id, safe='')}",
                        params=dashboard_params,
                    )
                enriched = {
                    "teamId": team_id,
                    "teamName": team.get("name"),
                    **detail,
                }
                append_jsonl(self.raw / "dashboards.jsonl", enriched)
                key = f"{team_id}:{dashboard_id or dashboard.get('name')}"
                self.database.upsert_metadata("dashboards", key, enriched)
                self.manifest.increment("dashboards")
        self.database.commit()

    def export_wikis(self) -> None:
        logging.info("Exportando metadatos de wikis.")
        payload, _ = self.client.get_json(
            f"{self.encoded_project}/_apis/wiki/wikis"
        )
        for wiki in payload.get("value", []):
            append_jsonl(self.raw / "wikis.jsonl", wiki)
            key = str(wiki.get("id") or wiki.get("name"))
            self.database.upsert_metadata("wikis", key, wiki)
            self.manifest.increment("wikis")
        self.database.commit()

    def run(self) -> None:
        overwrite_json(
            self.checkpoint_path,
            {
                "last_successful_export_utc": None,
                "last_run_started_utc": self.manifest.started_utc,
                "last_run_finished_utc": None,
                "status": "running",
            },
        )
        self.write_manifest()

        # Los ficheros raw representan una instantánea limpia de esta ejecución.
        for path in self.raw.glob("*.jsonl"):
            path.unlink()
        team_area_paths_file = self.raw / "team_area_paths.json"
        if team_area_paths_file.exists():
            team_area_paths_file.unlink()

        teams: list[dict[str, Any]] = []
        ids: list[int] = []

        components = [
            ("project", self.export_project),
            ("teams", self.export_teams),
            ("classification_nodes", self.export_classification_nodes),
            ("work_item_types", self.export_work_item_types),
            ("fields", self.export_fields),
        ]

        for name, function in components:
            if not self.enabled(name):
                continue
            try:
                result = function()
                if name == "teams" and isinstance(result, list):
                    teams = result
            except Exception as error:
                self.record_error(name, error)

        if self.enabled("team_area_paths") and teams:
            try:
                self.export_team_area_paths(teams)
            except Exception as error:
                self.record_error("team_area_paths", error)

        if self.enabled("team_iterations") and teams:
            try:
                self.export_team_iterations(teams)
            except Exception as error:
                self.record_error("team_iterations", error)

        if self.enabled("work_items"):
            try:
                ids = self.get_work_item_ids()
                self.export_work_items(ids)
            except Exception as error:
                self.record_error("work_items", error)

        if ids and any(
            self.enabled(name) for name in ("revisions", "updates", "comments")
        ):
            self.export_work_item_history(ids)

        if self.enabled("queries"):
            try:
                self.export_queries()
            except Exception as error:
                self.record_error("queries", error)

        if self.enabled("dashboards") and teams:
            try:
                self.export_dashboards(teams)
            except Exception as error:
                self.record_error("dashboards", error)

        if self.enabled("wikis"):
            try:
                self.export_wikis()
            except Exception as error:
                self.record_error("wikis", error)

        self.manifest.finished_utc = utc_now()
        self.manifest.status = (
            "completed_with_errors" if self.manifest.errors else "completed"
        )
        self.write_manifest()
        self.database.close()

        overwrite_json(
            self.checkpoint_path,
            {
                "last_successful_export_utc": (
                    self.manifest.finished_utc
                    if not self.manifest.errors
                    else None
                ),
                "last_run_started_utc": self.manifest.started_utc,
                "last_run_finished_utc": self.manifest.finished_utc,
                "status": self.manifest.status,
            },
        )

        print()
        print(f"Exportación finalizada: {self.manifest.status}")
        print(f"Manifest: {self.manifest_path}")
        print(
            "Base SQLite: "
            f"{self.output / 'database' / 'azuredevops.db'}"
        )
        print(f"Errores registrados: {len(self.manifest.errors)}")


def load_config(path: Path) -> dict[str, Any]:
    if not path.exists():
        raise FileNotFoundError(f"No existe el fichero de configuración: {path}")

    with path.open("r", encoding="utf-8") as handle:
        config = yaml.safe_load(handle) or {}

    required = ("organization_url", "project")
    missing = [name for name in required if not config.get(name)]
    if missing:
        raise ValueError(
            "Faltan propiedades obligatorias en config.yaml: "
            + ", ".join(missing)
        )

    parsed = urlparse(config["organization_url"])
    if parsed.scheme != "https" or not parsed.netloc:
        raise ValueError("organization_url debe ser una URL HTTPS válida.")

    return config


def configure_logging(log_path: Path, verbose: bool) -> None:
    log_path.parent.mkdir(parents=True, exist_ok=True)
    level = logging.DEBUG if verbose else logging.INFO
    formatter = logging.Formatter(
        "%(asctime)s | %(levelname)s | %(message)s"
    )

    file_handler = logging.FileHandler(log_path, encoding="utf-8")
    file_handler.setFormatter(formatter)

    console_handler = logging.StreamHandler()
    console_handler.setFormatter(formatter)

    logging.basicConfig(
        level=level,
        handlers=[file_handler, console_handler],
    )


def parse_arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Exporta datos de Azure DevOps orientados a Azure Boards."
    )
    parser.add_argument(
        "--config",
        default="config.yaml",
        help="Ruta del fichero YAML de configuración.",
    )
    parser.add_argument(
        "--test",
        action="store_true",
        help="Comprueba acceso y permisos básicos sin exportar datos.",
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="Activa logs detallados.",
    )
    return parser.parse_args()


def main() -> int:
    arguments = parse_arguments()

    try:
        config = load_config(Path(arguments.config).resolve())
        output = Path(config.get("output_directory", "output")).resolve()
        configure_logging(
            output / "logs" / "export.log",
            arguments.verbose,
        )

        pat = os.environ.get("AZURE_DEVOPS_EXT_PAT", "").strip()
        if not pat:
            raise RuntimeError(
                "No existe la variable de entorno AZURE_DEVOPS_EXT_PAT. "
                "En PowerShell ejecuta: "
                '$env:AZURE_DEVOPS_EXT_PAT = "TU_PAT"'
            )

        exporter = Exporter(config, pat)
        if arguments.test:
            exporter.test_access()
            exporter.database.close()
        else:
            exporter.run()
        return 0

    except KeyboardInterrupt:
        logging.error("Ejecución cancelada por el usuario.")
        return 130
    except Exception as error:
        logging.exception("La ejecución ha fallado: %s", error)
        print(f"ERROR: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
