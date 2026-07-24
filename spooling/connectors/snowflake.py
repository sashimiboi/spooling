from __future__ import annotations

import time

import snowflake.connector as sf

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


class SnowflakeConnector(DataConnector):
    def __init__(self, config: ConnectionConfig):
        self.config = config
        self._conn: sf.SnowflakeConnection | None = None

    async def connect(self) -> None:
        try:
            self._conn = sf.connect(
                account=self.config.account,
                user=self.config.username,
                password=self.config.password,
                warehouse=self.config.warehouse,
                database=self.config.database,
                schema=self.config.schema or "PUBLIC",
                role=self.config.role,
                application="Spooling",
                timeout=60,
                client_session_keep_alive=True,
            )
        except sf.errors.DatabaseError as e:
            if "Authentication" in str(e) or "390100" in str(e):
                raise AuthenticationError(f"Snowflake authentication failed: {e}") from e
            raise ConnectionError(f"Failed to connect to Snowflake: {e}") from e

    async def disconnect(self) -> None:
        if self._conn:
            self._conn.close()
            self._conn = None

    def is_connected(self) -> bool:
        return self._conn is not None

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
            raise ConnectionError("Not connected to Snowflake")

        start = time.monotonic()
        try:
            cur = self._conn.cursor()
            cur.execute(sql, params or ())
            rows = cur.fetchall()
            fields = (
                [
                    FieldInfo(name=d.name, data_type=d.type_code)
                    for d in cur.description
                ]
                if cur.description
                else []
            )
            columns = [d.name for d in cur.description] if cur.description else []
            dict_rows = [dict(zip(columns, row)) for row in rows] if columns else []
            exec_time = int((time.monotonic() - start) * 1000)
            cur.close()
            return QueryResult(rows=dict_rows, fields=fields, row_count=len(dict_rows), execution_time=exec_time)
        except Exception as e:
            raise QueryError(str(e), sql) from e

    _TYPE_MAP = {
        "NUMBER": "number",
        "DECIMAL": "number",
        "INT": "integer",
        "INTEGER": "integer",
        "BIGINT": "bigint",
        "FLOAT": "float",
        "DOUBLE": "float",
        "VARCHAR": "text",
        "TEXT": "text",
        "BOOLEAN": "boolean",
        "DATE": "date",
        "TIMESTAMP": "timestamp",
        "TIMESTAMP_TZ": "timestamptz",
        "TIMESTAMP_LTZ": "timestamptz",
        "TIMESTAMP_NTZ": "timestamp",
        "TIME": "time",
        "VARIANT": "json",
        "OBJECT": "json",
        "ARRAY": "json",
    }

    async def list_schemas(self) -> list[str]:
        result = await self.query(
            "SELECT SCHEMA_NAME FROM INFORMATION_SCHEMA.SCHEMATA "
            "WHERE CATALOG_NAME = CURRENT_DATABASE() "
            "AND SCHEMA_NAME NOT IN ('INFORMATION_SCHEMA') ORDER BY SCHEMA_NAME"
        )
        return [r["SCHEMA_NAME"] for r in result.rows]

    async def list_tables(self, schema: str | None = None) -> list[TableInfo]:
        s = schema or "PUBLIC"
        result = await self.query(
            "SELECT TABLE_SCHEMA, TABLE_NAME, TABLE_TYPE FROM INFORMATION_SCHEMA.TABLES "
            "WHERE TABLE_CATALOG = CURRENT_DATABASE() "
            "AND TABLE_SCHEMA = %s ORDER BY TABLE_NAME",
            [s],
        )
        return [
            TableInfo(
                schema_name=r["TABLE_SCHEMA"],
                name=r["TABLE_NAME"],
                type=("view" if r["TABLE_TYPE"] == "VIEW" else "external" if "EXTERNAL" in r["TABLE_TYPE"] else "table"),
            )
            for r in result.rows
        ]

    async def get_table_columns(self, schema: str, table: str) -> list[ColumnInfo]:
        result = await self.query(
            "SELECT COLUMN_NAME, DATA_TYPE, IS_NULLABLE, COMMENT "
            "FROM INFORMATION_SCHEMA.COLUMNS "
            "WHERE TABLE_CATALOG = CURRENT_DATABASE() "
            "AND TABLE_SCHEMA = %s AND TABLE_NAME = %s "
            "ORDER BY ORDINAL_POSITION",
            [schema, table],
        )
        return [
            ColumnInfo(
                name=r["COLUMN_NAME"],
                data_type=self._TYPE_MAP.get(r["DATA_TYPE"].upper(), r["DATA_TYPE"]),
                nullable=r.get("IS_NULLABLE", "YES") == "YES",
                comment=r.get("COMMENT"),
            )
            for r in result.rows
        ]
