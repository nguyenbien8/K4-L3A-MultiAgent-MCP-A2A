from __future__ import annotations

import asyncio
import functools
import operator
from typing import Any, TypedDict, Annotated

from langgraph.graph import StateGraph, START, END

from .mcp_gateway import EvidenceGateway
from .trace import TraceWriter


# ─────────────────────────────────────────────────────────────
# State
# ─────────────────────────────────────────────────────────────

def _last(a, b): return b
def _merge_dicts(a: dict, b: dict) -> dict: return {**a, **b}


class AgentState(TypedDict):
    case: Annotated[dict[str, Any], _last]
    case_id: Annotated[str, _last]
    mcp_data: Annotated[dict[str, Any], _merge_dicts]
    evidence_refs: Annotated[list[str], operator.add]
    final_output: Annotated[dict[str, Any], _last]


# ─────────────────────────────────────────────────────────────
# Helper: gọi MCP + ghi trace
# ─────────────────────────────────────────────────────────────

async def _call_mcp(gateway, trace, tool_name, case_id, actor, max_retries=3, **kwargs):
    for attempt in range(max_retries):
        try:
            ev = await gateway.call(tool_name, case_id=case_id, **kwargs)
            trace.emit(
                case_id=case_id,
                event_type="tool_result_consumed",
                actor=actor,
                tool_name=tool_name,
                evidence_refs=[ev["evidence_ref"]],
            )
            return ev
        except Exception as e:
            err = str(e)
            if attempt < max_retries - 1 and ("server returned" in err.lower() or "500" in err or "503" in err or "502" in err):
                wait = 3 * (attempt + 1)
                print(f"[MCP Retry] {tool_name} attempt {attempt+2}/{max_retries} in {wait}s...")
                await asyncio.sleep(wait)
            else:
                print(f"[MCP Error] {tool_name}: {e}")
                return None


# ─────────────────────────────────────────────────────────────
# Specialist nodes: gọi MCP, trả về data thô
# ─────────────────────────────────────────────────────────────

async def order_node(state: AgentState, gateway, trace) -> AgentState:
    order_id = state["case"].get("customer_request", {}).get("claimed_order_id", "")
    mcp, ev_refs = {}, []
    for tool in ["get_order", "get_order_items"]:
        r = await _call_mcp(gateway, trace, tool, state["case_id"], "order-agent", order_id=order_id)
        if r:
            mcp[tool] = r["data"]
            ev_refs.append(r["evidence_ref"])
    return {"mcp_data": mcp, "evidence_refs": ev_refs}


async def payment_node(state: AgentState, gateway, trace) -> AgentState:
    order_id = state["case"].get("customer_request", {}).get("claimed_order_id", "")
    mcp, ev_refs = {}, []
    for tool in ["get_order_payments", "get_payment_timeline"]:
        r = await _call_mcp(gateway, trace, tool, state["case_id"], "payment-agent", order_id=order_id)
        if r:
            mcp[tool] = r["data"]
            ev_refs.append(r["evidence_ref"])
    return {"mcp_data": mcp, "evidence_refs": ev_refs}


async def shipment_node(state: AgentState, gateway, trace) -> AgentState:
    order_id = state["case"].get("customer_request", {}).get("claimed_order_id", "")
    mcp, ev_refs = {}, []
    r = await _call_mcp(gateway, trace, "get_shipment_summary", state["case_id"], "shipment-agent", order_id=order_id)
    if r:
        mcp["get_shipment_summary"] = r["data"]
        ev_refs.append(r["evidence_ref"])
    return {"mcp_data": mcp, "evidence_refs": ev_refs}


async def policy_node(state: AgentState, gateway, trace) -> AgentState:
    order_id = state["case"].get("customer_request", {}).get("claimed_order_id", "")
    policy_version = state["case"].get("policy_version", "")
    mcp, ev_refs = {}, []

    # get_policy requires policy_version
    if policy_version:
        r = await _call_mcp(gateway, trace, "get_policy", state["case_id"], "policy-agent", policy_version=policy_version)
        if r:
            mcp["get_policy"] = r["data"]
            ev_refs.append(r["evidence_ref"])

    # get_refund_timeline - try with order_id
    r = await _call_mcp(gateway, trace, "get_refund_timeline", state["case_id"], "policy-agent", order_id=order_id)
    if r:
        mcp["get_refund_timeline"] = r["data"]
        ev_refs.append(r["evidence_ref"])

    trace.emit(case_id=state["case_id"], event_type="policy_decided", actor="policy-agent", decision_code="policy_applied")
    return {"mcp_data": mcp, "evidence_refs": ev_refs}


# ─────────────────────────────────────────────────────────────
# Rule Engine (no LLM)
# ─────────────────────────────────────────────────────────────

def _detect_primary_issue(claims: list[dict], mcp_data: dict) -> str:
    """Determine primary issue from claims + MCP evidence."""
    claim_topics = {c.get("topic", "") for c in claims}
    order = mcp_data.get("get_order", {})
    payments = mcp_data.get("get_order_payments", {})
    shipment = mcp_data.get("get_shipment_summary", {})
    refund_timeline = mcp_data.get("get_refund_timeline", {})

    order_status = str(order.get("order_status", "")).lower()
    payment_list = payments.get("payments", []) if isinstance(payments, dict) else []
    total_paid = sum(float(p.get("payment_value", 0)) for p in payment_list if isinstance(p, dict))

    # Rule 1: Canceled order with payment
    if ("canceled_order_paid" in claim_topics or order_status == "canceled") and total_paid > 0:
        return "canceled_order_paid"

    # Rule 2: Unavailable order with payment
    if "unavailable_order_paid" in claim_topics or order_status == "unavailable":
        return "unavailable_order_paid"

    # Rule 3: Late delivery - check shipment
    if "late_delivery" in claim_topics or any("late" in t for t in claim_topics):
        carrier = str(shipment.get("carrier", "")).lower() if isinstance(shipment, dict) else ""
        if carrier:
            return "late_delivery_logistics"
        return "late_delivery_seller"

    # Rule 4: Payment mismatch
    if "payment_mismatch" in claim_topics:
        return "payment_mismatch"

    # Rule 5: Duplicate charge
    if "duplicate_charge" in claim_topics:
        return "duplicate_charge"

    # Rule 6: Refund requested
    if "requested_full_refund" in claim_topics or "refund_requested" in claim_topics:
        if refund_timeline:
            refund_status = str(refund_timeline.get("status", "")).lower() if isinstance(refund_timeline, dict) else ""
            if "failed" in refund_status:
                return "refund_failed"
            return "refund_pending"
        if order_status == "canceled" and total_paid > 0:
            return "canceled_order_paid"

    return "insufficient_evidence"


def _detect_responsible_party(primary_issue: str, mcp_data: dict) -> list[dict]:
    mapping = {
        "canceled_order_paid": "seller",
        "unavailable_order_paid": "seller",
        "late_delivery_seller": "seller",
        "late_delivery_logistics": "logistics_provider",
        "payment_mismatch": "payment_provider",
        "duplicate_charge": "payment_provider",
        "refund_pending": "platform",
        "refund_failed": "payment_provider",
        "insufficient_evidence": "unknown",
        "unsupported_claim": "unknown",
    }
    party_type = mapping.get(primary_issue, "unknown")
    return [{"party_type": party_type, "party_id": None}]


def _calc_refund(primary_issue: str, mcp_data: dict) -> tuple[float, list[dict]]:
    """Returns (total_refund_brl, refund_lines)."""
    payments = mcp_data.get("get_order_payments", {})
    items = mcp_data.get("get_order_items", {})
    payment_list = payments.get("payments", []) if isinstance(payments, dict) else []
    total_paid = sum(float(p.get("payment_value", 0)) for p in payment_list if isinstance(p, dict))

    no_refund_issues = {"insufficient_evidence", "unsupported_claim", "late_delivery_logistics",
                        "late_delivery_seller", "valid_split_payment"}
    if primary_issue in no_refund_issues and primary_issue not in {"canceled_order_paid", "unavailable_order_paid"}:
        if primary_issue not in {"canceled_order_paid", "unavailable_order_paid",
                                  "payment_mismatch", "duplicate_charge", "refund_pending", "refund_failed"}:
            return 0.0, []

    if total_paid > 0:
        return round(total_paid, 2), [{"reason_code": primary_issue, "amount_brl": round(total_paid, 2), "entity_id": None}]
    return 0.0, []


def _extract_affected_entities(mcp_data: dict, order_id: str) -> dict:
    order = mcp_data.get("get_order", {})
    items_data = mcp_data.get("get_order_items", {})
    payments = mcp_data.get("get_order_payments", {})
    shipment = mcp_data.get("get_shipment_summary", {})

    item_ids = []
    if isinstance(items_data, dict):
        for item in items_data.get("items", []):
            if isinstance(item, dict) and item.get("order_item_id"):
                item_ids.append(str(item["order_item_id"]))

    seller_ids = []
    if isinstance(items_data, dict):
        for item in items_data.get("items", []):
            if isinstance(item, dict) and item.get("seller_id"):
                seller_ids.append(str(item["seller_id"]))

    payment_refs = []
    if isinstance(payments, dict):
        for p in payments.get("payments", []):
            if isinstance(p, dict) and p.get("sequential"):
                payment_refs.append(f"{order_id}_payment_{p['sequential']}")

    shipment_ids = []
    if isinstance(shipment, dict) and shipment.get("order_id"):
        shipment_ids.append(str(shipment.get("order_id", "")))

    return {
        "order_ids": [order_id] if order_id else [],
        "item_ids": list(set(item_ids))[:20],
        "seller_ids": list(set(seller_ids))[:20],
        "payment_references": list(set(payment_refs))[:20],
        "shipment_ids": list(set(shipment_ids))[:20],
    }


def _calc_confidence(primary_issue: str, mcp_data: dict, evidence_refs: list) -> float:
    """Confidence based on how much evidence we have."""
    evidence_count = len(evidence_refs)
    if evidence_count == 0:
        return 0.1
    if primary_issue == "insufficient_evidence":
        return 0.2
    # More evidence = more confidence, capped at 0.9
    base = min(0.5 + evidence_count * 0.1, 0.9)
    # Bump if order data confirms the claim
    order_status = str(mcp_data.get("get_order", {}).get("order_status", "")).lower()
    if primary_issue == "canceled_order_paid" and order_status == "canceled":
        base = min(base + 0.1, 0.95)
    return round(base, 2)


# ─────────────────────────────────────────────────────────────
# Verifier: Rule-based synthesis — zero LLM calls
# ─────────────────────────────────────────────────────────────

async def verifier_node(state: AgentState, gateway, trace) -> AgentState:
    case = state["case"]
    mcp_data = state.get("mcp_data", {})
    ev_refs = state.get("evidence_refs", [])
    claims = case.get("customer_request", {}).get("claims", [])
    order_id = case.get("customer_request", {}).get("claimed_order_id", "")

    primary_issue = _detect_primary_issue(claims, mcp_data)
    responsible = _detect_responsible_party(primary_issue, mcp_data)
    refund_total, refund_lines = _calc_refund(primary_issue, mcp_data)
    entities = _extract_affected_entities(mcp_data, order_id)
    confidence = _calc_confidence(primary_issue, mcp_data, ev_refs)

    # Determine case_status
    if primary_issue == "insufficient_evidence":
        case_status = "needs_investigation"
    elif refund_total > 0:
        case_status = "action_required"
    else:
        case_status = "no_action"

    # Build claim assessments from case input
    claim_assessments = []
    for c in claims[:5]:
        verdict = "supported" if primary_issue != "insufficient_evidence" else "insufficient_evidence"
        claim_assessments.append({
            "claim_id": c.get("claim_id", ""),
            "verdict": verdict,
            "confidence": confidence,
            "evidence_refs": ev_refs[:5],
        })

    # Resolution actions (≤80 chars each)
    resolution_actions = []
    if refund_total > 0:
        resolution_actions.append(f"Refund BRL {refund_total:.2f} to original payment method")
    if primary_issue == "canceled_order_paid":
        resolution_actions.append("Close cancelled order and confirm refund")
    elif primary_issue in ("refund_pending", "refund_failed"):
        resolution_actions.append("Escalate refund to payment provider")

    final = {
        "schema_version": "day09-l3a-output-v2",
        "case_id": state["case_id"],
        "assessment": {
            "primary_issue": primary_issue,
            "case_status": case_status,
            "confidence": confidence,
        },
        "affected_entities": entities,
        "claim_assessments": claim_assessments,
        "root_cause_analysis": {
            "ranked_causes": [{"cause_code": primary_issue.upper().replace("-", "_"), "rank": 1}],
            "responsible_parties": responsible,
        },
        "evidence_refs": ev_refs[:30],
        "data_conflicts": [],
        "financial_resolution": {
            "currency": "BRL",
            "recommended_refund_brl": refund_total,
            "refund_lines": refund_lines,
        },
        "resolution_actions": [a[:80] for a in resolution_actions[:8]],
    }

    trace.emit(case_id=state["case_id"], event_type="verification_completed", actor="verifier", decision_code="verified")
    return {"final_output": final}


# ─────────────────────────────────────────────────────────────
# Build Graph
# ─────────────────────────────────────────────────────────────

def build_graph(gateway, trace):
    wf = StateGraph(AgentState)
    p = functools.partial

    wf.add_node("order",    p(order_node,    gateway=gateway, trace=trace))
    wf.add_node("payment",  p(payment_node,  gateway=gateway, trace=trace))
    wf.add_node("shipment", p(shipment_node, gateway=gateway, trace=trace))
    wf.add_node("policy",   p(policy_node,   gateway=gateway, trace=trace))
    wf.add_node("verifier", p(verifier_node, gateway=gateway, trace=trace))

    # Sequential: tránh BrokenResourceError do parallel MCP calls trên 1 SSE connection
    wf.add_edge(START,      "order")
    wf.add_edge("order",    "payment")
    wf.add_edge("payment",  "shipment")
    wf.add_edge("shipment", "policy")
    wf.add_edge("policy",   "verifier")
    wf.add_edge("verifier", END)

    return wf.compile()


async def solve_case(
    case: dict[str, Any], gateway: EvidenceGateway, trace: TraceWriter
) -> dict[str, Any]:
    trace.emit(case_id=case["case_id"], event_type="case_received", actor="system")
    trace.emit(case_id=case["case_id"], event_type="task_assigned",  actor="coordinator", target="specialists")
    trace.emit(case_id=case["case_id"], event_type="handoff",        actor="coordinator", target="verifier")

    graph = build_graph(gateway, trace)
    initial: AgentState = {
        "case": case, "case_id": case["case_id"],
        "mcp_data": {}, "evidence_refs": [], "final_output": {},
    }
    result = await graph.ainvoke(initial)

    trace.emit(case_id=case["case_id"], event_type="case_finalized", actor="system", decision_code="output_generated")
    return result.get("final_output", {})
