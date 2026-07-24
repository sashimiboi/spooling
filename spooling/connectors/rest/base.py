from __future__ import annotations

import re
import time
from abc import abstractmethod
from typing import Any

import httpx

from spooling.connectors.base import DataConnector
from spooling.connectors.types import (
    AuthenticationError,
    ColumnInfo,
    ConnectionConfig,
    ConnectionError,
    FieldInfo,
    QueryError,
    QueryResult,
    TableInfo,
    TestConnectionResult,
)


class RESTResource:
    def __init__(
        self,
        name: str,
        description: str,
        endpoint: str,
        method: str = "GET",
        params: dict[str, str] | None = None,
        fields: list[dict[str, str]] | None = None,
        pagination: dict[str, Any] | None = None,
    ):
        self.name = name
        self.description = description
        self.endpoint = endpoint
        self.method = method
        self.params = params or {}
        self.fields = fields or []
        self.pagination = pagination


class RESTConnector(DataConnector):
    def __init__(self, config: ConnectionConfig):
        self.config = config
        self._connected = False

    @abstractmethod
    def get_resources(self) -> list[RESTResource]: ...

    @abstractmethod
    def build_headers(self) -> dict[str, str]: ...

    @abstractmethod
    def get_test_endpoint(self) -> tuple[str, callable]: ...

    def get_display_name(self) -> str:
        return self.config.name

    async def connect(self) -> None:
        self._connected = True

    async def disconnect(self) -> None:
        self._connected = False

    def is_connected(self) -> bool:
        return self._connected

    async def test_connection(self) -> TestConnectionResult:
        start = time.monotonic()
        url, validator = self.get_test_endpoint()
        try:
            async with httpx.AsyncClient(timeout=10) as client:
                resp = await client.get(url, headers=self.build_headers())
            if resp.status_code in (401, 403):
                raise AuthenticationError("Invalid API key or insufficient permissions")
            if not resp.is_success:
                text = resp.text[:200]
                raise ConnectionError(f"API returned {resp.status_code}: {text}")
            data = resp.json()
            valid = validator(data)
            return TestConnectionResult(
                success=valid,
                message=f"Connected to {self.get_display_name()} ({int((time.monotonic() - start) * 1000)}ms)"
                if valid
                else "Connected but response validation failed",
                details={"response_time": int((time.monotonic() - start) * 1000)},
            )
        except (AuthenticationError, ConnectionError):
            raise
        except Exception as e:
            raise ConnectionError(f"Failed to connect to {self.get_display_name()}: {e}") from e

    async def list_schemas(self) -> list[str]:
        return [self.config.type]

    async def list_tables(self, schema: str | None = None) -> list[TableInfo]:
        return [
            TableInfo(schema_name=self.config.type, name=r.name, description=r.description)
            for r in self.get_resources()
        ]

    async def get_table_columns(self, schema: str, table: str) -> list[ColumnInfo]:
        for r in self.get_resources():
            if r.name == table:
                return [
                    ColumnInfo(name=f["name"], data_type=f.get("dataType", "string"), nullable=True, comment=f.get("description"))
                    for f in r.fields
                ]
        return []

    async def query(self, sql: str, params: list | None = None) -> QueryResult:
        start = time.monotonic()
        parsed = self._parse_simple_query(sql)
        resources = self.get_resources()
        resource = next((r for r in resources if r.name == parsed["table"]), None)
        if not resource:
            available = ", ".join(r.name for r in resources)
            raise QueryError(f"Unknown resource: {parsed['table']}. Available: {available}")

        rows = await self._fetch_resource(resource, parsed["limit"])
        fields = [FieldInfo(name=f["name"], data_type=f.get("dataType", "string")) for f in resource.fields]

        field_names = (
            [f["name"] for f in resource.fields]
            if parsed["fields"] == "*"
            else parsed["fields"]
        )

        selected = []
        for row in rows[: parsed["limit"]]:
            out = {}
            for f in field_names:
                out[f] = row.get(f, self._get_nested(row, f))
            selected.append(out)

        return QueryResult(
            rows=selected,
            fields=fields,
            row_count=len(selected),
            execution_time=int((time.monotonic() - start) * 1000),
        )

    async def _fetch_resource(self, resource: RESTResource, limit: int) -> list[dict[str, Any]]:
        url = resource.endpoint
        if not url.startswith("http"):
            base = self.config.custom_properties.get("base_url") if self.config.custom_properties else ""
            url = base.rstrip("/") + "/" + url.lstrip("/")

        params = dict(resource.params)
        pagination = resource.pagination
        if pagination and pagination.get("limit_param"):
            params[pagination["limit_param"]] = str(min(limit, pagination.get("default_limit", 100)))

        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.request(
                resource.method or "GET",
                url,
                headers=self.build_headers(),
                params=params,
            )
        if not resp.is_success:
            raise QueryError(f"{self.get_display_name()} API error {resp.status_code}: {resp.text[:300]}")

        data = resp.json()
        data_path = pagination.get("data_path") if pagination else None
        items = self._get_nested(data, data_path) if data_path else data
        if not isinstance(items, list):
            items = [self._flatten(data)]
        return [self._flatten(item) for item in items]

    def _flatten(self, obj: Any, prefix: str = "") -> dict[str, Any]:
        result = {}
        if not isinstance(obj, dict):
            return {prefix.lstrip("."): obj}
        for key, value in obj.items():
            full_key = f"{prefix}.{key}" if prefix else key
            if isinstance(value, dict):
                result.update(self._flatten(value, full_key))
            else:
                result[full_key] = value
        return result

    def _get_nested(self, obj: Any, path: str) -> Any:
        parts = path.split(".")
        current = obj
        for part in parts:
            if current is None or not isinstance(current, dict):
                return None
            current = current.get(part)
        return current

    def _parse_simple_query(self, sql: str) -> dict[str, Any]:
        clean = sql.strip().rstrip(";").strip()
        m = re.match(r"^SELECT\s+(.+?)\s+FROM\s+(\w+)(?:\s+LIMIT\s+(\d+))?\s*$", clean, re.IGNORECASE)
        if not m:
            resource = next((r for r in self.get_resources() if r.name == clean), None)
            if resource:
                return {"table": clean, "fields": "*", "limit": 25}
            raise QueryError("Cannot parse query. Use: SELECT * FROM <resource> [LIMIT n]")
        fields_raw = m.group(1).strip()
        table = m.group(2)
        limit = int(m.group(3)) if m.group(3) else 25
        fields = "*" if fields_raw == "*" else [f.strip() for f in fields_raw.split(",")]
        return {"table": table, "fields": fields, "limit": limit}
