from __future__ import annotations

import time

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


class MongoDBConnector(DataConnector):
    def __init__(self, config: ConnectionConfig):
        self.config = config
        self._client = None
        self._db = None

    async def connect(self) -> None:
        try:
            from motor.motor_asyncio import AsyncIOMotorClient

            uri = self.config.custom_properties.get("uri") if self.config.custom_properties else None
            if not uri:
                host = self.config.host or "localhost"
                port = self.config.port or 27017
                user = self.config.username or ""
                pwd = self.config.password or ""
                if user and pwd:
                    uri = f"mongodb://{user}:{pwd}@{host}:{port}/{self.config.database or 'admin'}"
                else:
                    uri = f"mongodb://{host}:{port}/{self.config.database or 'admin'}"

            self._client = AsyncIOMotorClient(uri, serverSelectionTimeoutMS=10000)
            await self._client.admin.command("ping")
            self._db = self._client[self.config.database or "admin"]
        except Exception as e:
            msg = str(e).lower()
            if "auth" in msg or "unauthorized" in msg:
                raise AuthenticationError(f"MongoDB authentication failed: {e}") from e
            raise ConnectionError(f"Failed to connect to MongoDB: {e}") from e

    async def disconnect(self) -> None:
        if self._client:
            self._client.close()
            self._client = None
            self._db = None

    def is_connected(self) -> bool:
        return self._client is not None

    async def test_connection(self) -> TestConnectionResult:
        try:
            if not self.is_connected():
                await self.connect()
            return TestConnectionResult(success=True, message="Connection successful.")
        except Exception as e:
            return TestConnectionResult(success=False, message=str(e))

    async def query(self, sql: str, params: list | None = None) -> QueryResult:
        raise QueryError(
            "MongoDB does not support SQL queries via this connector. "
            "Use list_tables / get_table_columns for schema discovery."
        )

    async def list_schemas(self) -> list[str]:
        if not self._client:
            raise ConnectionError("Not connected to MongoDB")
        dbs = await self._client.list_database_names()
        return [d for d in dbs if d not in ("admin", "local", "config")]

    async def list_tables(self, schema: str | None = None) -> list[TableInfo]:
        if not self._client:
            raise ConnectionError("Not connected to MongoDB")
        db_name = schema or (self.config.database or "admin")
        db = self._client[db_name]
        collections = await db.list_collection_names()
        return [
            TableInfo(schema_name=db_name, name=c, type="table")
            for c in sorted(collections)
        ]

    async def get_table_columns(self, schema: str, table: str) -> list[ColumnInfo]:
        if not self._client:
            raise ConnectionError("Not connected to MongoDB")
        db = self._client[schema]
        sample = await db[table].find_one()
        if not sample:
            return []
        return [
            ColumnInfo(
                name=k,
                data_type=type(v).__name__,
                nullable=True,
            )
            for k, v in sample.items()
        ]
