from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal


DatabaseType = Literal[
    "snowflake", "postgresql", "bigquery", "clickhouse", "mongodb", "mysql", "redshift"
]


@dataclass
class ConnectionConfig:
    type: DatabaseType
    name: str
    host: str | None = None
    port: int | None = None
    database: str | None = None
    username: str | None = None
    password: str | None = None
    ssl: bool = False
    account: str | None = None
    warehouse: str | None = None
    schema_: str | None = None
    role: str | None = None
    project_id: str | None = None
    dataset_id: str | None = None
    service_account: dict[str, Any] | None = None
    custom_properties: dict[str, Any] | None = None

    @property
    def schema(self) -> str | None:
        return self.schema_


@dataclass
class FieldInfo:
    name: str
    data_type: str
    nullable: bool | None = None


@dataclass
class QueryResult:
    rows: list[dict[str, Any]]
    fields: list[FieldInfo]
    row_count: int
    execution_time: int


@dataclass
class TableInfo:
    schema_name: str
    name: str
    type: Literal["table", "view", "external"] = "table"
    row_count: int | None = None
    description: str | None = None


@dataclass
class ColumnInfo:
    name: str
    data_type: str
    nullable: bool = True
    is_primary_key: bool = False
    comment: str | None = None


@dataclass
class TestConnectionResult:
    success: bool
    message: str
    details: dict[str, Any] | None = None


class ConnectionError(Exception):
    pass


class QueryError(Exception):
    def __init__(self, message: str, sql: str | None = None):
        super().__init__(message)
        self.sql = sql


class AuthenticationError(Exception):
    pass
