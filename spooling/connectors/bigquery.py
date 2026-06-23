from __future__ import annotations

import json
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

# BigQuery uses google-cloud-bigquery which is synchronous.
# We wrap in run_in_executor for async compatibility.


class BigQueryConnector(DataConnector):
    def __init__(self, config: ConnectionConfig):
        self.config = config
        self._client = None

    async def _get_client(self):
        if self._client is None:
            from google.cloud import bigquery
            from google.oauth2 import service_account

            if self.config.service_account:
                credentials = service_account.Credentials.from_service_account_info(
                    self.config.service_account
                )
                self._client = bigquery.Client(
                    project=self.config.project_id or credentials.project_id,
                    credentials=credentials,
                )
            else:
                self._client = bigquery.Client(project=self.config.project_id)
        return self._client

    async def connect(self) -> None:
        import asyncio

        try:
            client = await self._get_client()
            await asyncio.to_thread(client.query, "SELECT 1")
        except Exception as e:
            msg = str(e).lower()
            if "unauthorized" in msg or "invalid" in msg or "denied" in msg:
                raise AuthenticationError(f"BigQuery authentication failed: {e}") from e
            raise ConnectionError(f"Failed to connect to BigQuery: {e}") from e

    async def disconnect(self) -> None:
        if self._client:
            self._client.close()
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
        import asyncio

        client = await self._get_client()
        start = time.monotonic()
        try:
            job = await asyncio.to_thread(client.query, sql)
            rows_iter = await asyncio.to_thread(job.result)
            rows = [dict(r.items()) for r in rows_iter]
            fields = (
                [
                    FieldInfo(name=s.name, data_type=s.field_type)
                    for s in rows_iter.schema
                ]
                if rows_iter.schema
                else []
            )
            exec_time = int((time.monotonic() - start) * 1000)
            return QueryResult(rows=rows, fields=fields, row_count=len(rows), execution_time=exec_time)
        except Exception as e:
            raise QueryError(str(e), sql) from e

    async def list_schemas(self) -> list[str]:
        import asyncio

        client = await self._get_client()
        datasets = await asyncio.to_thread(list, client.list_datasets())
        return [d.dataset_id for d in datasets]

    async def list_tables(self, schema: str | None = None) -> list[TableInfo]:
        import asyncio

        client = await self._get_client()
        dataset_id = schema or self.config.dataset_id
        if not dataset_id:
            raise QueryError("dataset_id is required")
        tables = await asyncio.to_thread(list, client.list_tables(dataset_id))
        return [
            TableInfo(
                schema_name=dataset_id,
                name=t.table_id,
                type="table" if t.table_type == "TABLE" else "view",
            )
            for t in tables
        ]

    async def get_table_columns(self, schema: str, table: str) -> list[ColumnInfo]:
        import asyncio

        client = await self._get_client()
        dataset_id = schema or self.config.dataset_id
        ref = client.dataset(dataset_id).table(table)
        tbl = await asyncio.to_thread(client.get_table, ref)
        return [
            ColumnInfo(
                name=s.name,
                data_type=s.field_type,
                nullable=s.is_nullable,
            )
            for s in tbl.schema
        ]
