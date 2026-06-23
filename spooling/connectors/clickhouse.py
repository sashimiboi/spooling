from __future__ import annotations

import time

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


class ClickHouseConnector(DataConnector):
    def __init__(self, config: ConnectionConfig):
        self.config = config
        self._client = None

    async def connect(self) -> None:
        try:
            import clickhouse_connect

            self._client = clickhouse_connect.get_client(
                host=self.config.host or "localhost",
                port=self.config.port or 8123,
                database=self.config.database or "default",
                username=self.config.username or "default",
                password=self.config.password or "",
            )
            self._client.query("SELECT 1")
        except Exception as e:
            msg = str(e).lower()
            if "auth" in msg or "denied" in msg or "unauthorized" in msg:
                raise AuthenticationError(f"ClickHouse authentication failed: {e}") from e
            raise ConnectionError(f"Failed to connect to ClickHouse: {e}") from e

    async def disconnect(self) -> None:
        self._client = None

    def is_connected(self) -> bool:
        return self._client is not None

    async def test_connection(self) -> TestConnectionResult:
        try:
            if not self.is_connected():
                await self.connect()
            result = await self.query("SELECT 1 AS ok")
            return TestConnectionResult(
                success=True,
                message="Connection successful.",
                details={"row_count": result.row_count},
            )
        except Exception as e:
            return TestConnectionResult(success=False, message=str(e))

    async def query(self, sql: str, params: list | None = None) -> QueryResult:
        if not self._client:
            raise ConnectionError("Not connected to ClickHouse")
        start = time.monotonic()
        try:
            result = self._client.query(sql, parameters=params or ())
            rows = result.result_rows or []
            columns = result.column_names or []
            fields = [FieldInfo(name=c, data_type="unknown") for c in columns]
            dict_rows = [dict(zip(columns, r)) for r in rows] if columns else []
            exec_time = int((time.monotonic() - start) * 1000)
            return QueryResult(rows=dict_rows, fields=fields, row_count=len(dict_rows), execution_time=exec_time)
        except Exception as e:
            raise QueryError(str(e), sql) from e

    async def list_schemas(self) -> list[str]:
        result = await self.query("SELECT name FROM system.databases ORDER BY name")
        return [r["name"] for r in result.rows]

    async def list_tables(self, schema: str | None = None) -> list[TableInfo]:
        s = schema or (self.config.database or "default")
        result = await self.query(
            "SELECT database, name, engine FROM system.tables WHERE database = %s ORDER BY name",
            [s],
        )
        return [
            TableInfo(
                schema_name=r["database"],
                name=r["name"],
                type="table",
            )
            for r in result.rows
        ]

    async def get_table_columns(self, schema: str, table: str) -> list[ColumnInfo]:
        result = await self.query(
            "SELECT name, type, is_in_primary_key FROM system.columns "
            "WHERE database = %s AND table = %s ORDER BY position",
            [schema, table],
        )
        return [
            ColumnInfo(
                name=r["name"],
                data_type=r["type"],
                nullable=True,
                is_primary_key=r.get("is_in_primary_key", False),
            )
            for r in result.rows
        ]
