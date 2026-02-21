"""
demos.support.tools — Production tools for the customer support pipeline.

Each tool is registered via the ``@tool`` decorator and uses realistic mock
data. In production, these would hit real databases, ticket systems, and APIs.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from typing import Any

from sdk.internal.tools import tool

# ---------------------------------------------------------------------------
# Mock data — realistic customer/order/ticket corpus
# ---------------------------------------------------------------------------

_CUSTOMERS: dict[str, dict[str, Any]] = {
    "cust_001": {
        "id": "cust_001",
        "name": "Acme Corp",
        "email": "billing@acme.com",
        "plan": "enterprise",
        "mrr": 4999.00,
        "status": "active",
        "created_at": "2024-03-15",
        "account_manager": "Sarah Chen",
        "tags": ["high-value", "api-heavy"],
    },
    "cust_002": {
        "id": "cust_002",
        "name": "StartupXYZ",
        "email": "ops@startupxyz.io",
        "plan": "growth",
        "mrr": 299.00,
        "status": "active",
        "created_at": "2025-01-10",
        "account_manager": "Mike Torres",
        "tags": ["self-serve"],
    },
    "cust_003": {
        "id": "cust_003",
        "name": "DataFlow Inc",
        "email": "support@dataflow.dev",
        "plan": "enterprise",
        "mrr": 8500.00,
        "status": "active",
        "created_at": "2023-11-01",
        "account_manager": "Sarah Chen",
        "tags": ["high-value", "soc2", "dedicated-infra"],
    },
    "cust_004": {
        "id": "cust_004",
        "name": "FreeUser LLC",
        "email": "hello@freeuser.com",
        "plan": "free",
        "mrr": 0.00,
        "status": "churned",
        "created_at": "2025-06-01",
        "account_manager": None,
        "tags": ["free-tier"],
    },
}

_ORDERS: dict[str, list[dict[str, Any]]] = {
    "cust_001": [
        {
            "order_id": "ord_5501",
            "product": "Platform Enterprise",
            "amount": 4999.00,
            "status": "active",
            "billing_cycle": "monthly",
            "next_billing": "2026-03-15",
            "last_payment": "2026-02-15",
            "payment_status": "paid",
        },
        {
            "order_id": "ord_5502",
            "product": "Premium Support Add-on",
            "amount": 500.00,
            "status": "active",
            "billing_cycle": "monthly",
            "next_billing": "2026-03-15",
            "last_payment": "2026-02-15",
            "payment_status": "paid",
        },
    ],
    "cust_002": [
        {
            "order_id": "ord_7201",
            "product": "Platform Growth",
            "amount": 299.00,
            "status": "active",
            "billing_cycle": "monthly",
            "next_billing": "2026-03-10",
            "last_payment": "2026-02-10",
            "payment_status": "paid",
        },
    ],
    "cust_003": [
        {
            "order_id": "ord_3301",
            "product": "Platform Enterprise",
            "amount": 8500.00,
            "status": "active",
            "billing_cycle": "annual",
            "next_billing": "2026-11-01",
            "last_payment": "2025-11-01",
            "payment_status": "paid",
        },
    ],
    "cust_004": [
        {
            "order_id": "ord_9901",
            "product": "Platform Free",
            "amount": 0.00,
            "status": "cancelled",
            "billing_cycle": "none",
            "next_billing": None,
            "last_payment": None,
            "payment_status": "n/a",
        },
    ],
}

_PAST_TICKETS: list[dict[str, Any]] = [
    {
        "ticket_id": "TKT-1001",
        "customer_id": "cust_001",
        "subject": "Double charge on February invoice",
        "category": "billing",
        "severity": "P2",
        "status": "resolved",
        "resolution": "Refund issued for duplicate charge. Root cause: payment gateway retry on timeout.",
        "created_at": "2026-01-20",
        "resolved_at": "2026-01-21",
    },
    {
        "ticket_id": "TKT-1002",
        "customer_id": "cust_001",
        "subject": "API rate limit exceeded during peak hours",
        "category": "technical",
        "severity": "P3",
        "status": "resolved",
        "resolution": "Rate limit increased from 1000 to 5000 req/min for enterprise tier.",
        "created_at": "2026-01-05",
        "resolved_at": "2026-01-06",
    },
    {
        "ticket_id": "TKT-1003",
        "customer_id": "cust_002",
        "subject": "Cannot access dashboard after password reset",
        "category": "account",
        "severity": "P2",
        "status": "resolved",
        "resolution": "Session cache cleared. User able to log in after clearing browser cookies.",
        "created_at": "2026-02-01",
        "resolved_at": "2026-02-01",
    },
    {
        "ticket_id": "TKT-1004",
        "customer_id": "cust_003",
        "subject": "Webhook delivery failures to endpoint",
        "category": "technical",
        "severity": "P1",
        "status": "resolved",
        "resolution": "Customer's SSL certificate expired. Guided them through renewal. Replayed missed webhooks.",
        "created_at": "2026-02-10",
        "resolved_at": "2026-02-11",
    },
    {
        "ticket_id": "TKT-1005",
        "customer_id": "cust_003",
        "subject": "Request for SOC2 compliance documentation",
        "category": "general",
        "severity": "P4",
        "status": "resolved",
        "resolution": "Sent SOC2 Type II report and data processing addendum.",
        "created_at": "2026-02-15",
        "resolved_at": "2026-02-15",
    },
]

_KB_ARTICLES: list[dict[str, Any]] = [
    {
        "id": "kb_001",
        "title": "Handling Duplicate Charges",
        "content": (
            "When a customer reports a duplicate charge: 1) Verify in Stripe dashboard. "
            "2) Check payment gateway logs for retry events. 3) If confirmed duplicate, "
            "issue refund via billing admin. 4) Notify customer with refund confirmation "
            "and expected processing time (3-5 business days)."
        ),
        "category": "billing",
        "tags": ["refund", "duplicate", "charge", "stripe"],
    },
    {
        "id": "kb_002",
        "title": "API Rate Limiting Policy",
        "content": (
            "Rate limits by tier: Free=100 req/min, Growth=1000 req/min, "
            "Enterprise=5000 req/min (expandable). To request an increase: "
            "enterprise customers contact their AM, growth customers upgrade tier. "
            "Rate limit headers: X-RateLimit-Limit, X-RateLimit-Remaining, X-RateLimit-Reset."
        ),
        "category": "technical",
        "tags": ["api", "rate-limit", "throttling"],
    },
    {
        "id": "kb_003",
        "title": "Account Access Recovery",
        "content": (
            "For login issues: 1) Clear browser cookies and cache. 2) Try incognito mode. "
            "3) If SSO, verify IdP configuration. 4) For persistent issues, admin can "
            "force-reset session via Admin Console > Users > Reset Sessions."
        ),
        "category": "account",
        "tags": ["login", "access", "sso", "password"],
    },
    {
        "id": "kb_004",
        "title": "Webhook Troubleshooting Guide",
        "content": (
            "Common webhook failures: 1) SSL certificate errors — customer must renew. "
            "2) Endpoint timeout (>30s) — optimize handler. 3) 4xx/5xx responses — "
            "check endpoint logs. Webhook replay: available for last 72 hours via "
            "API or dashboard. Max retry attempts: 5 with exponential backoff."
        ),
        "category": "technical",
        "tags": ["webhook", "ssl", "retry", "delivery"],
    },
    {
        "id": "kb_005",
        "title": "Escalation Policy",
        "content": (
            "Escalation criteria: P1 = service outage (immediate), P2 = degraded service "
            "(4h SLA), P3 = minor issue (24h SLA), P4 = general inquiry (48h SLA). "
            "Enterprise customers with Premium Support: all severities get 2x faster SLA. "
            "Escalation path: Agent → Team Lead → Engineering On-Call → VP Engineering."
        ),
        "category": "process",
        "tags": ["escalation", "sla", "priority", "severity"],
    },
]

# ---------------------------------------------------------------------------
# Registered tools
# ---------------------------------------------------------------------------


@tool(
    "search_tickets",
    "Search past support tickets by keyword, customer ID, or category. "
    "Returns matching tickets with resolution history.",
    {
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": "Search query (keyword, ticket ID, or description fragment)",
            },
            "customer_id": {
                "type": "string",
                "description": "Filter by customer ID (e.g. cust_001)",
            },
            "category": {
                "type": "string",
                "description": "Filter by category: billing, technical, account, general",
                "enum": ["billing", "technical", "account", "general"],
            },
            "limit": {
                "type": "integer",
                "description": "Max results to return (default 5)",
            },
        },
        "required": ["query"],
    },
    required_tier="public",
)
def search_tickets(
    query: str,
    customer_id: str = "",
    category: str = "",
    limit: int = 5,
) -> str:
    """Search past tickets with keyword matching and optional filters."""
    query_lower = query.lower()
    results: list[dict[str, Any]] = []

    for ticket in _PAST_TICKETS:
        # Keyword match
        searchable = (
            f"{ticket['subject']} {ticket['resolution']} {ticket['category']}"
        ).lower()
        if query_lower not in searchable and query_lower != ticket["ticket_id"].lower():
            continue

        # Filters
        if customer_id and ticket["customer_id"] != customer_id:
            continue
        if category and ticket["category"] != category:
            continue

        results.append(ticket)
        if len(results) >= limit:
            break

    return json.dumps({"matches": results, "total": len(results)})


@tool(
    "query_customer",
    "Look up customer account information by customer ID. "
    "Returns plan, MRR, status, tags, and account manager.",
    {
        "type": "object",
        "properties": {
            "customer_id": {
                "type": "string",
                "description": "The customer ID (e.g. cust_001)",
            },
        },
        "required": ["customer_id"],
    },
    required_tier="public",
)
def query_customer(customer_id: str) -> str:
    """Look up customer account details."""
    customer = _CUSTOMERS.get(customer_id)
    if customer is None:
        return json.dumps({"error": f"Customer {customer_id} not found"})
    return json.dumps(customer)


@tool(
    "check_order_status",
    "Query order and subscription status for a customer. "
    "Returns all active/cancelled orders with billing details.",
    {
        "type": "object",
        "properties": {
            "customer_id": {
                "type": "string",
                "description": "The customer ID (e.g. cust_001)",
            },
        },
        "required": ["customer_id"],
    },
    required_tier="public",
)
def check_order_status(customer_id: str) -> str:
    """Get order/subscription status for a customer."""
    orders = _ORDERS.get(customer_id)
    if orders is None:
        return json.dumps({"error": f"No orders found for {customer_id}"})
    return json.dumps({"customer_id": customer_id, "orders": orders})


@tool(
    "search_knowledge_base",
    "Search internal knowledge base articles by keyword or category. "
    "Returns relevant articles with troubleshooting steps.",
    {
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": "Search query (e.g. 'duplicate charge', 'webhook failure')",
            },
            "category": {
                "type": "string",
                "description": "Filter by category: billing, technical, account, process",
            },
        },
        "required": ["query"],
    },
    required_tier="public",
)
def search_knowledge_base(query: str, category: str = "") -> str:
    """Search KB articles with keyword matching."""
    query_lower = query.lower()
    results: list[dict[str, Any]] = []

    for article in _KB_ARTICLES:
        searchable = f"{article['title']} {article['content']} {' '.join(article['tags'])}".lower()
        if query_lower not in searchable:
            # Check individual query words
            words = query_lower.split()
            if not any(w in searchable for w in words):
                continue

        if category and article["category"] != category:
            continue

        results.append(article)

    return json.dumps({"articles": results, "total": len(results)})


@tool(
    "escalate_to_human",
    "Flag a ticket for human agent handoff. Use when the issue requires "
    "manual intervention, involves sensitive data, or exceeds agent authority.",
    {
        "type": "object",
        "properties": {
            "reason": {
                "type": "string",
                "description": "Why this ticket needs human review",
            },
            "severity": {
                "type": "string",
                "description": "Ticket severity: P1, P2, P3, P4",
                "enum": ["P1", "P2", "P3", "P4"],
            },
            "suggested_team": {
                "type": "string",
                "description": "Recommended team: billing, engineering, account-management, security",
                "enum": ["billing", "engineering", "account-management", "security"],
            },
        },
        "required": ["reason", "severity"],
    },
    required_tier="public",
)
def escalate_to_human(
    reason: str, severity: str, suggested_team: str = "account-management"
) -> str:
    """Create an escalation record for human handoff."""
    escalation = {
        "escalation_id": f"ESC-{datetime.now(UTC).strftime('%Y%m%d%H%M%S')}",
        "reason": reason,
        "severity": severity,
        "suggested_team": suggested_team,
        "created_at": datetime.now(UTC).isoformat(),
        "status": "pending_review",
        "sla_deadline": (
            datetime.now(UTC)
            + timedelta(hours={"P1": 1, "P2": 4, "P3": 24, "P4": 48}.get(severity, 48))
        ).isoformat(),
    }
    return json.dumps(escalation)


@tool(
    "send_response",
    "Send the final customer-facing response. This is an irreversible action "
    "that delivers the message to the customer.",
    {
        "type": "object",
        "properties": {
            "customer_id": {
                "type": "string",
                "description": "The customer ID to respond to",
            },
            "subject": {
                "type": "string",
                "description": "Response subject line",
            },
            "body": {
                "type": "string",
                "description": "The full response body (plain text or markdown)",
            },
            "internal_notes": {
                "type": "string",
                "description": "Internal notes (not visible to customer)",
            },
        },
        "required": ["customer_id", "subject", "body"],
    },
    required_tier="privileged",
)
def send_response(
    customer_id: str,
    subject: str,
    body: str,
    internal_notes: str = "",
) -> str:
    """Send the final response to the customer (mock — logs instead of sending)."""
    response_record = {
        "response_id": f"RSP-{datetime.now(UTC).strftime('%Y%m%d%H%M%S')}",
        "customer_id": customer_id,
        "subject": subject,
        "body": body,
        "internal_notes": internal_notes,
        "sent_at": datetime.now(UTC).isoformat(),
        "channel": "email",
        "status": "delivered",
    }
    return json.dumps(response_record)


# ---------------------------------------------------------------------------
# Tool collection helper
# ---------------------------------------------------------------------------

ALL_TOOLS = [
    search_tickets,
    query_customer,
    check_order_status,
    search_knowledge_base,
    escalate_to_human,
    send_response,
]


def get_tool_specs() -> list[Any]:
    """Return ToolSpec objects for all support tools."""
    return [t.spec for t in ALL_TOOLS]  # type: ignore[attr-defined]
