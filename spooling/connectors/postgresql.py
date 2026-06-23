from __future__ import annotations

import time

import psycopg
from psycopg.rows import dict_row

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


class PostgreSQLConnector(DataConnector):
    def __init__(self, config: ConnectionConfig):
        self.config = config
        self._conn = None

    def _dsn(self) -> str:
        dsn = (
            f"host={self.config.host} port={self.config.port or 5432} "
            f"dbname={self.config.database} user={self.config.username} "
            f"password={self.config.password}"
        )
        if self.config.ssl:
            dsn += " sslmode=require"
        return dsn

    async def connect(self) -> None:
        try:
            self._conn = psycopg.connect(self._dsn(), row_factory=dict_row)
            self._conn.execute("SELECT 1")
        except psycopg.errors.InvalidPassword as e:
            raise AuthenticationError(f"PostgreSQL authentication failed: {e}") from e
        except Exception as e:
            raise ConnectionError(f"Failed to connect to PostgreSQL: {e}") from e

    async def disconnect(self) -> None:
        if self._conn:
            self._conn.close()
            self._conn = None

    def is_connected(self) -> bool:
        return self._conn is not None and not self._conn.closed

    async def test_connection(self) -> TestConnectionResult:
        try:
            if not self.is_connected():
                await self.connect()
            result = await self.query("SELECT 1 AS ok")
            return TestConnectionResult(
                success=True,
                message="Connection successful. Credentials are valid.",
                details={"row_count": result.row_count},
            )
        except Exception as e:
            return TestConnectionResult(success=False, message=str(e))

    async def query(self, sql: str, params: list | None = None) -> QueryResult:
        if not self._conn:
            raise ConnectionError("Not connected to PostgreSQL")
        start = time.monotonic()
        try:
            result = self._conn.execute(sql, params or ())
            rows = result.fetchall() if result.description else []
            fields = (
                [FieldInfo(name=d.name, data_type=self._oid_to_type(d.type_code)) for d in result.description]
                if result.description
                else []
            )
            row_count = len(rows) if rows else 0
            exec_time = int((time.monotonic() - start) * 1000)
            return QueryResult(rows=rows, fields=fields, row_count=row_count, execution_time=exec_time)
        except Exception as e:
            raise QueryError(str(e), sql) from e

    def _oid_to_type(self, oid: int) -> str:
        mapping = {
            16: "boolean", 20: "bigint", 21: "smallint", 23: "integer", 25: "text",
            700: "real", 701: "double precision", 1043: "varchar", 1082: "date",
            1083: "time", 1114: "timestamp", 1184: "timestamptz", 1700: "numeric",
            3802: "jsonb", 114: "json",
        }
        return mapping.get(oid, "unknown")

    async def list_schemas(self) -> list[str]:
        result = await self.query(
            "SELECT schema_name FROM information_schema.schemata "
            "WHERE schema_name NOT IN ('pg_catalog', 'information_schema', 'pg_toast') "
            "ORDER BY schema_name"
        )
        return [r["schema_name"] for r in result.rows]

    async def list_tables(self, schema: str | None = None) -> list[TableInfo]:
        s = schema or "public"
        result = await self.query(
            "SELECT t.schemaname AS schema, t.tablename AS name, s.n_live_tup AS row_count "
            "FROM pg_tables t LEFT JOIN pg_stat_user_tables s ON s.schemaname = t.schemaname AND s.relname = t.tablename "
            "WHERE t.schemaname = %s ORDER BY t.tablename", [s],
        )
        return [TableInfo(schema_name=r["schema"], name=r["name"], type="table", row_count=int(r.get("row_count") or 0)) for r in result.rows]

    async def get_table_columns(self, schema: str, table: str) -> list[ColumnInfo]:
        result = await self.query(
            "SELECT a.attname AS column_name, pg_catalog.format_type(a.atttypid, a.atttypmod) AS data_type, "
            "a.attnotnull AS not_null, CASE WHEN pk.contype = 'p' THEN true ELSE false END AS is_primary_key "
            "FROM pg_catalog.pg_attribute a JOIN pg_catalog.pg_class c ON c.oid = a.attrelid "
            "JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace "
            "LEFT JOIN pg_constraint pk ON pk.conrelid = c.oid AND pk.contype = 'p' AND a.attnum = ANY(pk.conkey) "
            "WHERE n.nspname = %s AND c.relname = %s AND a.attnum > 0 AND NOT a.attisdropped ORDER BY a.attnum", [schema, table],
        )
        return [ColumnInfo(name=r["column_name"], data_type=r["data_type"], nullable=not r["not_null"], is_primary_key=r["is_primary_key"]) for r in result.rows]
