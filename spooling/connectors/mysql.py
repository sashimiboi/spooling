from __future__ import annotations

import time

import mysql.connector

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


class MySQLConnector(DataConnector):
    def __init__(self, config: ConnectionConfig):
        self.config = config
        self._conn = None

    async def connect(self) -> None:
        try:
            self._conn = mysql.connector.connect(
                host=self.config.host,
                port=self.config.port or 3306,
                database=self.config.database,
                user=self.config.username,
                password=self.config.password,
                ssl_disabled=not self.config.ssl,
                connection_timeout=10,
            )
        except mysql.connector.errors.ProgrammingError as e:
            if "access" in str(e).lower() or "denied" in str(e).lower():
                raise AuthenticationError(f"MySQL authentication failed: {e}") from e
            raise ConnectionError(f"Failed to connect to MySQL: {e}") from e
        except Exception as e:
            raise ConnectionError(f"Failed to connect to MySQL: {e}") from e

    async def disconnect(self) -> None:
        if self._conn:
            self._conn.close()
            self._conn = None

    def is_connected(self) -> bool:
        return self._conn is not None and self._conn.is_connected()

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
        if not self._conn:
            raise ConnectionError("Not connected to MySQL")
        start = time.monotonic()
        try:
            cur = self._conn.cursor(dictionary=True)
            cur.execute(sql, params or ())
            rows = cur.fetchall() if cur.description else []
            fields = (
                [FieldInfo(name=d[0], data_type=self._type_name(d[1])) for d in cur.description]
                if cur.description
                else []
            )
            exec_time = int((time.monotonic() - start) * 1000)
            cur.close()
            return QueryResult(rows=rows, fields=fields, row_count=len(rows), execution_time=exec_time)
        except Exception as e:
            raise QueryError(str(e), sql) from e

    def _type_name(self, type_code) -> str:
        try:
            import mysql.connector

            return mysql.connector.FieldType.get_info(type_code)
        except Exception:
            return "unknown"

    async def list_schemas(self) -> list[str]:
        result = await self.query("SELECT SCHEMA_NAME FROM INFORMATION_SCHEMA.SCHEMATA ORDER BY SCHEMA_NAME")
        return [r["SCHEMA_NAME"] for r in result.rows]

    async def list_tables(self, schema: str | None = None) -> list[TableInfo]:
        s = schema or self.config.database or "public"
        result = await self.query(
            "SELECT TABLE_SCHEMA, TABLE_NAME, TABLE_TYPE FROM INFORMATION_SCHEMA.TABLES "
            "WHERE TABLE_SCHEMA = %s ORDER BY TABLE_NAME",
            [s],
        )
        return [
            TableInfo(
                schema_name=r["TABLE_SCHEMA"],
                name=r["TABLE_NAME"],
                type="view" if r["TABLE_TYPE"] == "VIEW" else "table",
            )
            for r in result.rows
        ]

    async def get_table_columns(self, schema: str, table: str) -> list[ColumnInfo]:
        result = await self.query(
            "SELECT COLUMN_NAME, DATA_TYPE, IS_NULLABLE, COLUMN_COMMENT, COLUMN_KEY "
            "FROM INFORMATION_SCHEMA.COLUMNS "
            "WHERE TABLE_SCHEMA = %s AND TABLE_NAME = %s "
            "ORDER BY ORDINAL_POSITION",
            [schema, table],
        )
        return [
            ColumnInfo(
                name=r["COLUMN_NAME"],
                data_type=r["DATA_TYPE"],
                nullable=r.get("IS_NULLABLE", "YES") == "YES",
                is_primary_key=r.get("COLUMN_KEY", "") == "PRI",
                comment=r.get("COLUMN_COMMENT"),
            )
            for r in result.rows
        ]
