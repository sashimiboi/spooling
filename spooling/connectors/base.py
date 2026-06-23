from __future__ import annotations

from abc import ABC, abstractmethod

from .types import (
    ColumnInfo,
    ConnectionConfig,
    QueryResult,
    TableInfo,
    TestConnectionResult,
)


class DataConnector(ABC):
    @abstractmethod
    async def connect(self) -> None: ...

    @abstractmethod
    async def disconnect(self) -> None: ...

    @abstractmethod
    def is_connected(self) -> bool: ...

    @abstractmethod
    async def test_connection(self) -> TestConnectionResult: ...

    @abstractmethod
    async def query(self, sql: str, params: list | None = None) -> QueryResult: ...

    @abstractmethod
    async def list_schemas(self) -> list[str]: ...

    @abstractmethod
    async def list_tables(self, schema: str | None = None) -> list[TableInfo]: ...

    @abstractmethod
    async def get_table_columns(self, schema: str, table: str) -> list[ColumnInfo]: ...
