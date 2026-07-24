from __future__ import annotations

from spooling.connectors.rest.base import RESTConnector, RESTResource


class KlaviyoConnector(RESTConnector):
    def get_display_name(self) -> str:
        return "Klaviyo"

    def build_headers(self) -> dict[str, str]:
        api_key = ""
        if self.config.custom_properties:
            api_key = self.config.custom_properties.get("api_key", "")
        return {
            "Authorization": f"Klaviyo-API-Key {api_key}",
            "revision": "2024-10-15",
            "Accept": "application/json",
        }

    def get_test_endpoint(self) -> tuple[str, callable]:
        return ("https://a.klaviyo.com/api/accounts/", lambda d: isinstance(d, dict) and "data" in d)

    def get_resources(self) -> list[RESTResource]:
        return [
            RESTResource("profiles", "Customer profiles and subscriber data", "/api/profiles/",
                pagination={"type": "cursor", "limit_param": "page[size]", "default_limit": 20, "data_path": "data"},
                fields=[{"name": "id"}, {"name": "attributes.email", "dataType": "string"}, {"name": "attributes.first_name", "dataType": "string"}, {"name": "attributes.last_name", "dataType": "string"}, {"name": "attributes.phone_number", "dataType": "string"}, {"name": "attributes.created", "dataType": "datetime"}, {"name": "attributes.updated", "dataType": "datetime"}]),
            RESTResource("lists", "Email and SMS subscriber lists", "/api/lists/",
                pagination={"type": "cursor", "data_path": "data"},
                fields=[{"name": "id"}, {"name": "attributes.name", "dataType": "string"}, {"name": "attributes.created", "dataType": "datetime"}, {"name": "attributes.updated", "dataType": "datetime"}]),
            RESTResource("campaigns", "Email campaigns", "/api/campaigns/",
                params={"filter": "equals(messages.channel,'email')"},
                pagination={"type": "cursor", "data_path": "data"},
                fields=[{"name": "id"}, {"name": "attributes.name", "dataType": "string"}, {"name": "attributes.status", "dataType": "string"}, {"name": "attributes.created_at", "dataType": "datetime"}]),
            RESTResource("flows", "Automated flows", "/api/flows/",
                pagination={"type": "cursor", "data_path": "data"},
                fields=[{"name": "id"}, {"name": "attributes.name", "dataType": "string"}, {"name": "attributes.status", "dataType": "string"}, {"name": "attributes.created", "dataType": "datetime"}]),
            RESTResource("metrics", "Event metrics", "/api/metrics/",
                pagination={"type": "cursor", "data_path": "data"},
                fields=[{"name": "id"}, {"name": "attributes.name", "dataType": "string"}, {"name": "attributes.created", "dataType": "datetime"}]),
            RESTResource("segments", "Customer segments", "/api/segments/",
                pagination={"type": "cursor", "data_path": "data"},
                fields=[{"name": "id"}, {"name": "attributes.name", "dataType": "string"}, {"name": "attributes.created", "dataType": "datetime"}, {"name": "attributes.is_active", "dataType": "boolean"}]),
        ]


class StripeConnector(RESTConnector):
    def get_display_name(self) -> str:
        return "Stripe"

    def build_headers(self) -> dict[str, str]:
        key = ""
        if self.config.custom_properties:
            key = self.config.custom_properties.get("api_key", "")
        return {"Authorization": f"Bearer {key}"}

    def get_test_endpoint(self) -> tuple[str, callable]:
        return ("https://api.stripe.com/v1/balance", lambda d: isinstance(d, dict))

    def get_resources(self) -> list[RESTResource]:
        return [
            RESTResource("customers", "Customer records", "https://api.stripe.com/v1/customers",
                pagination={"type": "page", "limit_param": "limit", "default_limit": 10, "data_path": "data"},
                fields=[{"name": "id"}, {"name": "email", "dataType": "string"}, {"name": "name", "dataType": "string"}, {"name": "created", "dataType": "timestamp"}, {"name": "currency", "dataType": "string"}]),
            RESTResource("charges", "Charge transactions", "https://api.stripe.com/v1/charges",
                pagination={"type": "page", "limit_param": "limit", "default_limit": 10, "data_path": "data"},
                fields=[{"name": "id"}, {"name": "amount", "dataType": "number"}, {"name": "currency", "dataType": "string"}, {"name": "status", "dataType": "string"}, {"name": "created", "dataType": "timestamp"}, {"name": "customer", "dataType": "string"}]),
            RESTResource("subscriptions", "Subscription records", "https://api.stripe.com/v1/subscriptions",
                pagination={"type": "page", "limit_param": "limit", "default_limit": 10, "data_path": "data"},
                fields=[{"name": "id"}, {"name": "customer", "dataType": "string"}, {"name": "status", "dataType": "string"}, {"name": "created", "dataType": "timestamp"}, {"name": "current_period_start", "dataType": "timestamp"}, {"name": "current_period_end", "dataType": "timestamp"}]),
            RESTResource("invoices", "Invoice records", "https://api.stripe.com/v1/invoices",
                pagination={"type": "page", "limit_param": "limit", "default_limit": 10, "data_path": "data"},
                fields=[{"name": "id"}, {"name": "number", "dataType": "string"}, {"name": "customer", "dataType": "string"}, {"name": "total", "dataType": "number"}, {"name": "status", "dataType": "string"}, {"name": "created", "dataType": "timestamp"}]),
            RESTResource("products", "Product catalog", "https://api.stripe.com/v1/products",
                pagination={"type": "page", "limit_param": "limit", "default_limit": 10, "data_path": "data"},
                fields=[{"name": "id"}, {"name": "name", "dataType": "string"}, {"name": "active", "dataType": "boolean"}, {"name": "created", "dataType": "timestamp"}, {"name": "description", "dataType": "string"}]),
        ]


class HubSpotConnector(RESTConnector):
    def get_display_name(self) -> str:
        return "HubSpot"

    def build_headers(self) -> dict[str, str]:
        token = ""
        if self.config.custom_properties:
            token = self.config.custom_properties.get("access_token", "")
        return {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}

    def get_test_endpoint(self) -> tuple[str, callable]:
        return ("https://api.hubapi.com/account-info/v3/details", lambda d: isinstance(d, dict))

    def get_resources(self) -> list[RESTResource]:
        return [
            RESTResource("contacts", "Contact records", "https://api.hubapi.com/crm/v3/objects/contacts",
                pagination={"type": "page", "limit_param": "limit", "default_limit": 10, "data_path": "results"},
                fields=[{"name": "id"}, {"name": "properties.firstname", "dataType": "string"}, {"name": "properties.lastname", "dataType": "string"}, {"name": "properties.email", "dataType": "string"}, {"name": "properties.phone", "dataType": "string"}, {"name": "properties.createdate", "dataType": "datetime"}]),
            RESTResource("companies", "Company records", "https://api.hubapi.com/crm/v3/objects/companies",
                pagination={"type": "page", "limit_param": "limit", "default_limit": 10, "data_path": "results"},
                fields=[{"name": "id"}, {"name": "properties.name", "dataType": "string"}, {"name": "properties.domain", "dataType": "string"}, {"name": "properties.createdate", "dataType": "datetime"}]),
            RESTResource("deals", "Deal records", "https://api.hubapi.com/crm/v3/objects/deals",
                pagination={"type": "page", "limit_param": "limit", "default_limit": 10, "data_path": "results"},
                fields=[{"name": "id"}, {"name": "properties.dealname", "dataType": "string"}, {"name": "properties.amount", "dataType": "number"}, {"name": "properties.dealstage", "dataType": "string"}, {"name": "properties.createdate", "dataType": "datetime"}]),
            RESTResource("tickets", "Support tickets", "https://api.hubapi.com/crm/v3/objects/tickets",
                pagination={"type": "page", "limit_param": "limit", "default_limit": 10, "data_path": "results"},
                fields=[{"name": "id"}, {"name": "properties.subject", "dataType": "string"}, {"name": "properties.status", "dataType": "string"}, {"name": "properties.createdate", "dataType": "datetime"}]),
        ]


class ShopifyConnector(RESTConnector):
    def get_display_name(self) -> str:
        return "Shopify"

    def build_headers(self) -> dict[str, str]:
        token = ""
        domain = ""
        if self.config.custom_properties:
            token = self.config.custom_properties.get("access_token", "")
            domain = self.config.custom_properties.get("shop_domain", "")
        self._domain = domain
        return {"X-Shopify-Access-Token": token}

    def _base(self) -> str:
        domain = getattr(self, "_domain", "")
        return f"https://{domain}/admin/api/2024-01"

    def get_test_endpoint(self) -> tuple[str, callable]:
        return (f"{self._base()}/shop.json", lambda d: isinstance(d, dict) and "shop" in d)

    def get_resources(self) -> list[RESTResource]:
        base = self._base()
        return [
            RESTResource("orders", "Shopify orders", f"{base}/orders.json",
                pagination={"type": "page", "limit_param": "limit", "default_limit": 50, "data_path": "orders"},
                fields=[{"name": "id"}, {"name": "name", "dataType": "string"}, {"name": "email", "dataType": "string"}, {"name": "total_price", "dataType": "number"}, {"name": "financial_status", "dataType": "string"}, {"name": "fulfillment_status", "dataType": "string"}, {"name": "created_at", "dataType": "datetime"}]),
            RESTResource("products", "Shopify products", f"{base}/products.json",
                pagination={"type": "page", "limit_param": "limit", "default_limit": 50, "data_path": "products"},
                fields=[{"name": "id"}, {"name": "title", "dataType": "string"}, {"name": "vendor", "dataType": "string"}, {"name": "product_type", "dataType": "string"}, {"name": "status", "dataType": "string"}, {"name": "created_at", "dataType": "datetime"}, {"name": "variants", "dataType": "json"}]),
            RESTResource("customers", "Shopify customers", f"{base}/customers.json",
                pagination={"type": "page", "limit_param": "limit", "default_limit": 50, "data_path": "customers"},
                fields=[{"name": "id"}, {"name": "email", "dataType": "string"}, {"name": "first_name", "dataType": "string"}, {"name": "last_name", "dataType": "string"}, {"name": "orders_count", "dataType": "number"}, {"name": "total_spent", "dataType": "number"}, {"name": "created_at", "dataType": "datetime"}]),
            RESTResource("inventory_levels", "Inventory levels", f"{base}/inventory_levels.json",
                pagination={"type": "page", "limit_param": "limit", "default_limit": 50, "data_path": "inventory_levels"},
                fields=[{"name": "inventory_item_id"}, {"name": "location_id"}, {"name": "available", "dataType": "number"}, {"name": "updated_at", "dataType": "datetime"}]),
        ]


class GorgiasConnector(RESTConnector):
    def get_display_name(self) -> str:
        return "Gorgias"

    def build_headers(self) -> dict[str, str]:
        key = ""
        if self.config.custom_properties:
            key = self.config.custom_properties.get("api_key", "")
        return {"Authorization": f"Bearer {key}", "Content-Type": "application/json"}

    def get_test_endpoint(self) -> tuple[str, callable]:
        return ("https://api.gorgias.com/integrations", lambda d: isinstance(d, dict))

    def get_resources(self) -> list[RESTResource]:
        return [
            RESTResource("tickets", "Support tickets", "https://api.gorgias.com/tickets",
                pagination={"type": "page", "limit_param": "limit", "default_limit": 20, "data_path": "data"},
                fields=[{"name": "id"}, {"name": "subject", "dataType": "string"}, {"name": "status", "dataType": "string"}, {"name": "channel", "dataType": "string"}, {"name": "created_datetime", "dataType": "datetime"}, {"name": "customer.email", "dataType": "string"}, {"name": "customer.name", "dataType": "string"}]),
            RESTResource("customers", "Customer records", "https://api.gorgias.com/customers",
                pagination={"type": "page", "limit_param": "limit", "default_limit": 20, "data_path": "data"},
                fields=[{"name": "id"}, {"name": "email", "dataType": "string"}, {"name": "name", "dataType": "string"}, {"name": "created_datetime", "dataType": "datetime"}]),
            RESTResource("messages", "Ticket messages", "https://api.gorgias.com/messages",
                pagination={"type": "page", "limit_param": "limit", "default_limit": 20, "data_path": "data"},
                fields=[{"name": "id"}, {"name": "ticket_id"}, {"name": "channel", "dataType": "string"}, {"name": "source.from", "dataType": "string"}, {"name": "body_text", "dataType": "text"}, {"name": "created_datetime", "dataType": "datetime"}]),
        ]
