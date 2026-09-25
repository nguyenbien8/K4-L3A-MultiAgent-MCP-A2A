from __future__ import annotations

import asyncio
from collections import Counter
from collections.abc import Iterable
from decimal import Decimal
from typing import Any

from .mcp_gateway import EvidenceGateway
from .trace import TraceWriter

RETRYABLE = (TimeoutError, ConnectionError, RuntimeError)
MCP_ATTEMPTS = 3
MCP_BACKOFF_SECONDS = 1.0
PAYMENT_ISSUES = {
    "canceled_order_paid",
    "unavailable_order_paid",
    "valid_split_payment",
    "payment_mismatch",
    "duplicate_charge",
    "refund_pending",
    "refund_failed",
}
SHIPMENT_ISSUES = {"late_delivery_seller", "late_delivery_logistics"}
NO_ACTION_ISSUES = {"valid_split_payment", "unsupported_claim"}
CAUSES = {
    "canceled_order_paid": ("ORDER_CANCELED_AFTER_PAYMENT", "platform"),
    "unavailable_order_paid": ("ITEM_UNAVAILABLE_AFTER_PAYMENT", "seller"),
    "late_delivery_seller": ("SELLER_HANDOFF_DELAY", "seller"),
    "late_delivery_logistics": ("LOGISTICS_DELIVERY_DELAY", "logistics_provider"),
    "valid_split_payment": ("VALID_SPLIT_PAYMENT", "customer"),
    "payment_mismatch": ("PAYMENT_TOTAL_MISMATCH", "payment_provider"),
    "duplicate_charge": ("DUPLICATE_PAYMENT_CAPTURE", "payment_provider"),
    "refund_pending": ("REFUND_PENDING", "payment_provider"),
    "refund_failed": ("REFUND_FAILED", "payment_provider"),
    "unsupported_claim": ("CLAIM_NOT_SUPPORTED", "customer"),
}
TOOL_DOMAINS = {
    "get_order": "order",
    "get_order_items": "item",
    "get_sellers": "seller",
    "get_order_payments": "payment",
    "get_payment_timeline": "payment",
    "get_refund_timeline": "refund",
    "get_shipment_summary": "shipment",
    "get_policy": "policy",
}
AGENT_TOOLS = {
    "order-item-agent": {"get_order", "get_order_items", "get_sellers"},
    "payment-agent": {"get_order_payments", "get_payment_timeline", "get_refund_timeline"},
    "shipment-agent": {"get_shipment_summary"},
    "policy-agent": {"get_policy"},
}
ISSUE_DOMAINS = {
    "canceled_order_paid": {"order", "payment", "policy"},
    "unavailable_order_paid": {"order", "item", "seller", "payment", "policy"},
    "late_delivery_seller": {"order", "item", "seller", "shipment", "policy"},
    "late_delivery_logistics": {"order", "shipment", "policy"},
    "valid_split_payment": {"order", "payment", "policy"},
    "payment_mismatch": {"order", "payment", "policy"},
    "duplicate_charge": {"order", "payment", "policy"},
    "refund_pending": {"payment", "refund", "policy"},
    "refund_failed": {"payment", "refund", "policy"},
    "unsupported_claim": {"order", "item", "payment", "shipment", "policy"},
    "insufficient_evidence": set(TOOL_DOMAINS.values()),
}
ACTIONS = {
    "canceled_order_paid": "issue_refund",
    "unavailable_order_paid": "issue_refund",
    "late_delivery_seller": "refund_freight",
    "late_delivery_logistics": "refund_freight",
    "valid_split_payment": "document_no_action",
    "payment_mismatch": "reconcile_payment",
    "duplicate_charge": "refund_duplicate_charge",
    "refund_pending": "monitor_refund",
    "refund_failed": "retry_refund",
    "unsupported_claim": "document_no_action",
}
CONFLICT_FIELDS = {
    "order_status",
    "payment_status",
    "shipment_status",
    "refund_status",
    "captured_total_brl",
    "payment_total",
    "order_total_brl",
}


def _walk(value: Any) -> Iterable[tuple[str, Any]]:
    if isinstance(value, dict):
        for key, item in value.items():
            yield key, item
            yield from _walk(item)
    elif isinstance(value, list):
        for item in value:
            yield from _walk(item)


def _dicts(value: Any) -> Iterable[dict[str, Any]]:
    if isinstance(value, dict):
        yield value
        for item in value.values():
            yield from _dicts(item)
    elif isinstance(value, list):
        for item in value:
            yield from _dicts(item)


def _values(evidence: Iterable[dict[str, Any]], keys: set[str]) -> list[Any]:
    return [value for item in evidence for key, value in _walk(item["data"]) if key in keys]


def _decimal(value: Any) -> Decimal | None:
    try:
        return Decimal(str(value)) if not isinstance(value, bool) else None
    except (ValueError, TypeError):
        return None


def _records(evidence: Iterable[dict[str, Any]], domain: str) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    for item in evidence:
        if item["domain"] != domain:
            continue
        data = item["data"]
        if isinstance(data, list):
            records.extend(record for record in data if isinstance(record, dict))
    return records


def _policy_rule(evidence: Iterable[dict[str, Any]], issue: str) -> dict[str, Any]:
    for item in evidence:
        if item["domain"] == "policy" and isinstance(item["data"], dict):
            rules = item["data"].get("rules", {})
            if isinstance(rules, dict) and isinstance(rules.get(issue), dict):
                return rules[issue]
    return {}


def _ids(evidence: Iterable[dict[str, Any]], keys: set[str]) -> list[str]:
    result: list[str] = []
    for value in _values(evidence, keys):
        candidates = value if isinstance(value, list) else [value]
        result.extend(str(item) for item in candidates if isinstance(item, (str, int)))
    return list(dict.fromkeys(result))[:20]


def _money(evidence: Iterable[dict[str, Any]]) -> float:
    values = [_decimal(row.get("payment_value")) for row in _records(evidence, "payment")]
    total = sum((value for value in values if value is not None), Decimal())
    return float(total.quantize(Decimal("0.01")))


def _first_money(evidence: Iterable[dict[str, Any]], keys: set[str]) -> Decimal | None:
    for value in _values(evidence, keys):
        parsed = _decimal(value)
        if parsed is not None:
            return parsed
    return None


def _refund_amount(issue: str, evidence: Iterable[dict[str, Any]]) -> float:
    evidence = list(evidence)
    policy_amount = _decimal(_policy_rule(evidence, issue).get("refund_brl"))
    if policy_amount is not None:
        return float(max(policy_amount, Decimal()).quantize(Decimal("0.01")))
    explicit = _first_money(
        evidence,
        {"recommended_refund_brl", "refundable_total_brl", "refund_amount_brl"},
    )
    if explicit is not None:
        return float(max(explicit, Decimal()).quantize(Decimal("0.01")))
    if issue == "payment_mismatch":
        captured = _first_money(evidence, {"captured_total_brl", "captured_total"})
        expected = _first_money(evidence, {"order_total_brl", "order_total"})
        if captured is not None and expected is not None:
            return float(abs(captured - expected).quantize(Decimal("0.01")))
    if issue in {
        "canceled_order_paid",
        "unavailable_order_paid",
        "duplicate_charge",
        "refund_failed",
    }:
        return _money(evidence)
    return 0.0


def _data_conflicts(evidence: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    facts: dict[str, dict[str, set[str]]] = {}
    for item in evidence:
        domain = item["domain"]
        for field, value in _walk(item["data"]):
            if field in CONFLICT_FIELDS and isinstance(value, (str, int, float, bool)):
                facts.setdefault(field, {}).setdefault(domain, set()).add(str(value))
    conflicts: list[dict[str, Any]] = []
    for field, sources in facts.items():
        if len(sources) < 2 or len({value for values in sources.values() for value in values}) < 2:
            continue
        preferred = field.split("_", 1)[0]
        selected = preferred if preferred in sources else None
        conflicts.append(
            {
                "field": field,
                "sources": list(sources)[:5],
                "selected_source": selected,
                "resolution_code": (
                    "AUTHORITATIVE_DOMAIN_SELECTED" if selected else "MANUAL_REVIEW_REQUIRED"
                ),
            }
        )
    return conflicts[:5]


def _confidence(
    *, expected_count: int, evidence: list[dict[str, Any]], conflicts: list[dict[str, Any]]
) -> float:
    coverage = len(evidence) / expected_count if expected_count else 0.0
    warnings = sum(len(item.get("warnings", [])) for item in evidence)
    score = Decimal("0.35") + Decimal("0.60") * Decimal(str(coverage))
    score -= min(Decimal("0.30"), Decimal("0.15") * len(conflicts))
    if warnings:
        score -= Decimal("0.05")
    return float(min(Decimal("0.95"), max(Decimal("0.10"), score)).quantize(Decimal("0.01")))


def _observed_issue(evidence: Iterable[dict[str, Any]]) -> str:
    evidence = list(evidence)
    events = [
        record
        for item in evidence
        for record in _dicts(item["data"])
        if "event_type" in record
    ]
    refund = next((event for event in events if event["event_type"] == "refund_requested"), None)
    if refund and refund.get("status") == "failed":
        return "refund_failed"
    if refund and refund.get("status") == "pending":
        return "refund_pending"
    shipment = next((event for event in events if event["event_type"] == "delivered_late"), None)
    if shipment and shipment.get("actor") == "seller":
        return "late_delivery_seller"
    if shipment and shipment.get("actor") == "logistics_provider":
        return "late_delivery_logistics"
    order_statuses = _values(
        (item for item in evidence if item["domain"] == "order"), {"order_status"}
    )
    if "canceled" in order_statuses:
        return "canceled_order_paid"
    if "unavailable" in order_statuses:
        return "unavailable_order_paid"
    if any(event["event_type"] == "reconciliation_mismatch" for event in events):
        return "payment_mismatch"
    payments = _records(evidence, "payment")
    signatures = [
        (row.get("payment_sequential"), row.get("payment_type"), row.get("payment_value"))
        for row in payments
    ]
    if any(count > 1 for count in Counter(signatures).values()):
        return "duplicate_charge"
    if len({row.get("payment_sequential") for row in payments}) > 1:
        return "valid_split_payment"
    return "unsupported_claim"


def _relevant_refs(
    evidence: Iterable[dict[str, Any]], *, issue: str, claim_topic: str
) -> list[str]:
    domains = (
        {"payment", "refund", "policy"}
        if claim_topic == "requested_full_refund"
        else ISSUE_DOMAINS[issue]
    )
    return list(
        dict.fromkeys(item["evidence_ref"] for item in evidence if item["domain"] in domains)
    )


def _claim_verdict(topic: str, issue: str, complete: bool) -> str:
    if not complete:
        return "insufficient_evidence"
    if topic == issue:
        return "supported"
    if topic != "requested_full_refund":
        return "unsupported"
    if issue in {
        "canceled_order_paid",
        "unavailable_order_paid",
        "refund_pending",
        "refund_failed",
    }:
        return "supported"
    if issue in {
        "late_delivery_seller",
        "late_delivery_logistics",
        "payment_mismatch",
        "duplicate_charge",
    }:
        return "partially_supported"
    return "unsupported"


async def _evidence(
    gateway: EvidenceGateway,
    trace: TraceWriter,
    *,
    case_id: str,
    actor: str,
    tool_name: str,
    **arguments: str,
) -> dict[str, Any] | None:
    if tool_name not in AGENT_TOOLS[actor]:
        raise ValueError(f"{actor} is not allowed to call {tool_name}")
    for attempt in range(1, MCP_ATTEMPTS + 1):
        try:
            result = await asyncio.wait_for(
                gateway.call(tool_name, case_id=case_id, **arguments), timeout=30
            )
            if result["domain"] != TOOL_DOMAINS[tool_name]:
                raise ValueError(
                    f"{tool_name} returned domain {result['domain']!r}, "
                    f"expected {TOOL_DOMAINS[tool_name]!r}"
                )
            trace.emit(
                case_id=case_id,
                event_type="tool_result_consumed",
                actor=actor,
                tool_name=tool_name,
                evidence_refs=[result["evidence_ref"]],
                attributes={"domain": result["domain"], "attempt": attempt},
            )
            return result
        except RETRYABLE:
            if attempt == MCP_ATTEMPTS:
                return None
            # The gateway is occasionally slow or drops a call; back off before retrying.
            await asyncio.sleep(MCP_BACKOFF_SECONDS * attempt)
    return None


async def _specialist(
    gateway: EvidenceGateway,
    trace: TraceWriter,
    *,
    case_id: str,
    actor: str,
    calls: list[tuple[str, dict[str, str]]],
) -> list[dict[str, Any]]:
    trace.emit(case_id=case_id, event_type="task_assigned", actor="coordinator", target=actor)
    results = await asyncio.gather(
        *(
            _evidence(
                gateway,
                trace,
                case_id=case_id,
                actor=actor,
                tool_name=tool_name,
                **arguments,
            )
            for tool_name, arguments in calls
        )
    )
    evidence = [item for item in results if item is not None]
    trace.emit(
        case_id=case_id,
        event_type="handoff",
        actor=actor,
        target="policy-agent",
        decision_code="EVIDENCE_READY" if len(evidence) == len(calls) else "EVIDENCE_INCOMPLETE",
        evidence_refs=[item["evidence_ref"] for item in evidence],
    )
    return evidence


async def solve_case(
    case: dict[str, Any], gateway: EvidenceGateway, trace: TraceWriter
) -> dict[str, Any]:
    """Coordinate scoped specialists and return one contract-shaped L3A decision."""
    case_id = case["case_id"]
    request = case["customer_request"]
    order_id = request["claimed_order_id"]
    topics = [claim["topic"] for claim in request.get("claims", [])]
    hypothesis = next((topic for topic in topics if topic in CAUSES), "unsupported_claim")

    order_calls = [
        ("get_order", {"order_id": order_id}),
        ("get_order_items", {"order_id": order_id}),
    ]
    if hypothesis in {"unavailable_order_paid", "late_delivery_seller"}:
        order_calls.append(("get_sellers", {"order_id": order_id}))
    payment_calls = [("get_order_payments", {"order_id": order_id})]
    if hypothesis in PAYMENT_ISSUES:
        payment_calls.append(("get_payment_timeline", {"order_id": order_id}))
    if hypothesis in {"refund_pending", "refund_failed"}:
        payment_calls.append(("get_refund_timeline", {"order_id": order_id}))
    shipment_calls = (
        [("get_shipment_summary", {"order_id": order_id})] if hypothesis in SHIPMENT_ISSUES else []
    )

    order_evidence, payment_evidence, shipment_evidence = await asyncio.gather(
        _specialist(
            gateway, trace, case_id=case_id, actor="order-item-agent", calls=order_calls
        ),
        _specialist(
            gateway, trace, case_id=case_id, actor="payment-agent", calls=payment_calls
        ),
        _specialist(
            gateway, trace, case_id=case_id, actor="shipment-agent", calls=shipment_calls
        ),
    )
    trace.emit(
        case_id=case_id, event_type="task_assigned", actor="coordinator", target="policy-agent"
    )
    policy = await _evidence(
        gateway,
        trace,
        case_id=case_id,
        actor="policy-agent",
        tool_name="get_policy",
        policy_version=case["policy_version"],
    )
    evidence = [*order_evidence, *payment_evidence, *shipment_evidence]
    if policy is not None:
        evidence.append(policy)
    complete = (
        len(order_evidence) == len(order_calls)
        and len(payment_evidence) == len(payment_calls)
        and policy is not None
    )
    if hypothesis in SHIPMENT_ISSUES:
        complete = complete and len(shipment_evidence) == len(shipment_calls)
    observed = [*order_evidence, *payment_evidence, *shipment_evidence]
    issue = _observed_issue(observed) if complete else "insufficient_evidence"
    conflicts = _data_conflicts(observed)
    rule = _policy_rule(evidence, issue)
    unresolved_conflict = any(conflict["selected_source"] is None for conflict in conflicts)
    fallback_status = "no_action" if issue in NO_ACTION_ISSUES else "action_required"
    status = (
        "needs_investigation"
        if not complete or unresolved_conflict
        else rule.get("case_status", fallback_status)
    )
    expected_count = len(order_calls) + len(payment_calls) + len(shipment_calls) + 1
    confidence = _confidence(
        expected_count=expected_count, evidence=evidence, conflicts=conflicts
    )
    if not complete:
        confidence = min(confidence, 0.49)
    cause, fallback_party = CAUSES.get(issue, ("INSUFFICIENT_EVIDENCE", "unknown"))
    policy_parties = rule.get("responsible_parties", [])
    party_type = (
        policy_parties[0].get("party_type", fallback_party)
        if policy_parties and isinstance(policy_parties[0], dict)
        else fallback_party
    )
    seller_ids = _ids(evidence, {"seller_id", "seller_ids"})
    refundable = (
        _refund_amount(issue, evidence) if complete and status != "no_action" else 0.0
    )
    party_id = seller_ids[0] if party_type == "seller" and seller_ids else None
    recommended_action = rule.get("recommended_action", ACTIONS.get(issue))
    actions = (
        ["manual_evidence_review"]
        if not complete or unresolved_conflict
        else [recommended_action] if isinstance(recommended_action, str) else []
    )
    claim_assessments = [
        {
            "claim_id": claim["claim_id"],
            "verdict": _claim_verdict(claim["topic"], issue, complete),
            "confidence": confidence,
            "evidence_refs": _relevant_refs(
                evidence, issue=issue, claim_topic=claim["topic"]
            ),
        }
        for claim in request.get("claims", [])[:5]
    ]
    evidence_refs = list(dict.fromkeys(item["evidence_ref"] for item in evidence))

    trace.emit(
        case_id=case_id,
        event_type="policy_decided",
        actor="policy-agent",
        target="verifier-agent",
        decision_code=issue.upper(),
        evidence_refs=evidence_refs,
    )
    output: dict[str, Any] = {
        "schema_version": "day09-l3a-output-v2",
        "case_id": case_id,
        "assessment": {"primary_issue": issue, "case_status": status, "confidence": confidence},
        "affected_entities": {
            "order_ids": [order_id],
            "item_ids": _ids(evidence, {"order_item_id", "item_id", "item_ids"}),
            "seller_ids": seller_ids,
            "payment_references": _ids(
                evidence, {"payment_reference", "payment_id", "transaction_id"}
            ),
            "shipment_ids": _ids(evidence, {"shipment_id", "tracking_id"}),
        },
        "claim_assessments": claim_assessments,
        "root_cause_analysis": {
            "ranked_causes": [{"cause_code": cause, "rank": 1}],
            "responsible_parties": [{"party_type": party_type, "party_id": party_id}],
        },
        "evidence_refs": evidence_refs,
        "data_conflicts": conflicts,
        "financial_resolution": {
            "currency": "BRL",
            "recommended_refund_brl": refundable,
            "refund_lines": (
                [{"reason_code": issue.upper(), "amount_brl": refundable, "entity_id": order_id}]
                if refundable
                else []
            ),
        },
        "resolution_actions": actions,
    }
    refund_total = sum(
        line["amount_brl"] for line in output["financial_resolution"]["refund_lines"]
    )
    if refund_total != refundable:
        raise ValueError("refund line total does not match recommended refund")
    expected_party = CAUSES.get(issue, ("", "unknown"))[1]
    actual_party = output["root_cause_analysis"]["responsible_parties"][0]["party_type"]
    if actual_party != expected_party:
        raise ValueError(f"{issue} must assign responsibility to {expected_party}")
    if status == "no_action" and refundable:
        raise ValueError("no_action cases cannot recommend a refund")
    if conflicts and confidence > 0.8:
        raise ValueError("conflicting evidence cannot have confidence above 0.8")
    if not set(evidence_refs).issuperset(
        ref for claim in output["claim_assessments"] for ref in claim["evidence_refs"]
    ):
        raise ValueError("claim references evidence outside the case output")
    trace.emit(
        case_id=case_id,
        event_type="verification_completed",
        actor="verifier-agent",
        target="coordinator",
        decision_code="VALID",
        evidence_refs=evidence_refs,
    )
    return output
