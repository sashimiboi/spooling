from __future__ import annotations

from spooling.connectors.types import ConnectionConfig
from spooling.connectors.base import DataConnector
from spooling.connectors.postgresql import PostgreSQLConnector
from spooling.connectors.snowflake import SnowflakeConnector
from spooling.connectors.bigquery import BigQueryConnector
from spooling.connectors.mysql import MySQLConnector
from spooling.connectors.clickhouse import ClickHouseConnector
from spooling.connectors.mongodb import MongoDBConnector
from spooling.connectors.rest.base import RESTConnector
from spooling.connectors.rest.shopify_connectors import (
    KlaviyoConnector,
    StripeConnector,
    HubSpotConnector,
    ShopifyConnector,
    GorgiasConnector,
)

_DATABASE_CONNECTORS: dict[str, type] = {
    "postgresql": PostgreSQLConnector,
    "snowflake": SnowflakeConnector,
    "bigquery": BigQueryConnector,
    "mysql": MySQLConnector,
    "clickhouse": ClickHouseConnector,
    "mongodb": MongoDBConnector,
}

_REST_CONNECTORS: dict[str, type[RESTConnector]] = {
    "klaviyo": KlaviyoConnector,
    "stripe": StripeConnector,
    "hubspot": HubSpotConnector,
    "shopify": ShopifyConnector,
    "gorgias": GorgiasConnector,
}

ALL_CONNECTOR_TYPES: set[str] = set(_DATABASE_CONNECTORS.keys()) | set(_REST_CONNECTORS.keys())


def create_connector(config: ConnectionConfig) -> DataConnector:
    cls = _DATABASE_CONNECTORS.get(config.type)
    if cls:
        return cls(config)
    raise ValueError(f"Unsupported database type: {config.type}. Supported: {', '.join(_DATABASE_CONNECTORS.keys())}")


def create_rest_connector(type_name: str, credentials: dict[str, str]) -> RESTConnector:
    cls = _REST_CONNECTORS.get(type_name)
    if not cls:
        raise ValueError(f"Unsupported REST integration: {type_name}. Supported: {', '.join(_REST_CONNECTORS.keys())}")
    config = ConnectionConfig(
        type=type_name,  # type: ignore
        name=type_name,
        custom_properties=credentials,
    )
    return cls(config)


def is_rest_type(type_name: str) -> bool:
    return type_name in _REST_CONNECTORS


def get_supported_types() -> list[str]:
    return list(_DATABASE_CONNECTORS.keys())


def get_supported_rest_types() -> list[str]:
    return list(_REST_CONNECTORS.keys())
