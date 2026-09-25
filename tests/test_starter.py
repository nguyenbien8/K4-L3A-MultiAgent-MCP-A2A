from __future__ import annotations

import asyncio
import copy
import json
from pathlib import Path

import pytest

from student_agent import OUTPUT_SCHEMA_VERSION, VARIANT_ID
from student_agent.cases import CaseSet, load_case_set
from student_agent.contracts import Contracts
from student_agent.submission import REQUIRED_LIFECYCLE, _validate_lifecycle, build_manifest
from student_agent.workflow import _confidence, _data_conflicts, _observed_issue, solve_case


def write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value), encoding="utf-8")


def test_load_case_set_rejects_wrong_variant(tmp_path: Path) -> None:
    write_json(
        tmp_path / "case-set.json",
        {"case_set_version": "test-v1", "variant_id": "l3b", "case_ids": ["CASE_001"]},
    )
    write_json(tmp_path / "inputs" / "CASE_001.json", {"case_id": "CASE_001"})
    with pytest.raises(ValueError, match="expected variant"):
        load_case_set(tmp_path, expected_count=1)


def test_load_case_set_accepts_exact_input_inventory(tmp_path: Path) -> None:
    case_ids = ["CASE_001", "CASE_002"]
    write_json(
        tmp_path / "case-set.json",
        {"case_set_version": "test-v1", "variant_id": VARIANT_ID, "case_ids": case_ids},
    )
    for case_id in case_ids:
        write_json(tmp_path / "inputs" / f"{case_id}.json", {"case_id": case_id})
    loaded = load_case_set(tmp_path, expected_count=2)
    assert loaded.case_ids == tuple(case_ids)


def test_generated_manifest_matches_public_contract() -> None:
    root = Path(__file__).resolve().parents[1]
    contracts = Contracts(root / "contracts" / "schemas")
    case_set = CaseSet("test-v1", VARIANT_ID, ("CASE_001",), {})
    manifest = build_manifest(case_set)
    contracts.validate_manifest(manifest)
    assert manifest["output_schema_version"] == OUTPUT_SCHEMA_VERSION


def test_public_contracts_reject_unknown_fields() -> None:
    root = Path(__file__).resolve().parents[1]
    contracts = Contracts(root / "contracts" / "schemas")
    case_set = CaseSet("test-v1", VARIANT_ID, ("CASE_001",), {})
    manifest = build_manifest(case_set)
    manifest["extra"] = True
    with pytest.raises(ValueError, match="Additional properties"):
        contracts.validate_manifest(manifest)


def test_workflow_handoffs_valid_evidence_and_returns_schema_output() -> None:
    class Gateway:
        def __init__(self) -> None:
            self.calls: list[tuple[str, str, dict[str, str]]] = []
            self.responses: list[dict] = []

        async def call(self, tool_name: str, *, case_id: str, **arguments: str) -> dict:
            assert case_id == "CASE_001"
            self.calls.append((tool_name, case_id, arguments))
            domains = {
                "get_order": "order",
                "get_order_items": "item",
                "get_order_payments": "payment",
                "get_payment_timeline": "payment",
                "get_policy": "policy",
            }
            data = {
                "get_order": {"order_id": arguments.get("order_id"), "order_total_brl": 40.0},
                "get_order_items": {"items": [{"order_item_id": "ITEM_1"}]},
                "get_order_payments": [
                    {
                        "payment_sequential": "1",
                        "payment_type": "credit_card",
                        "payment_value": "35.00",
                    }
                ],
                "get_payment_timeline": {
                    "events": [
                        {
                            "transaction_id": "PAY_1",
                            "event_type": "reconciliation_mismatch",
                        }
                    ]
                },
                "get_policy": {
                    "policy_version": arguments.get("policy_version"),
                    "rules": {
                        "payment_mismatch": {
                            "case_status": "action_required",
                            "recommended_action": "reconcile_payment",
                            "refund_brl": 35.0,
                            "responsible_parties": [{"party_type": "payment_provider"}],
                        }
                    },
                },
            }[tool_name]
            response = {
                "schema_version": "day09-mcp-evidence-v1",
                "evidence_ref": f"ev_{tool_name.replace('_', ''):0<20}",
                "result_hash": f"sha256:{'0' * 64}",
                "domain": domains[tool_name],
                "data": data,
            }
            self.responses.append(copy.deepcopy(response))
            return response

    class Trace:
        def __init__(self) -> None:
            self.events: list[dict] = []

        def emit(self, **event: object) -> None:
            self.events.append(event)

    trace = Trace()
    gateway = Gateway()
    case = {
        "case_id": "CASE_001",
        "policy_version": "EC_POLICY_V1",
        "customer_request": {
            "claimed_order_id": "ORDER_1",
            "claims": [{"claim_id": "claim-1", "topic": "payment_mismatch"}],
        },
    }
    output = asyncio.run(solve_case(case, gateway, trace))  # type: ignore[arg-type]
    Contracts(Path(__file__).resolve().parents[1] / "contracts" / "schemas").validate_output(
        output, "output"
    )
    assert output["assessment"]["primary_issue"] == "payment_mismatch"
    assert output["assessment"]["confidence"] == 0.95
    assert output["financial_resolution"]["recommended_refund_brl"] == 35.0
    assert output["root_cause_analysis"]["responsible_parties"][0]["party_type"] == (
        "payment_provider"
    )
    assert output["resolution_actions"] == ["reconcile_payment"]
    assert {call[1] for call in gateway.calls} == {"CASE_001"}
    returned_refs = {response["evidence_ref"] for response in gateway.responses}
    consumed = {
        event["evidence_refs"][0]
        for event in trace.events
        if event["event_type"] == "tool_result_consumed"
    }
    assert consumed == returned_refs
    assert set(output["evidence_refs"]) == returned_refs
    permissions = {
        "order-item-agent": {"get_order", "get_order_items"},
        "payment-agent": {"get_order_payments", "get_payment_timeline"},
        "policy-agent": {"get_policy"},
    }
    for event in trace.events:
        if event["event_type"] == "tool_result_consumed":
            assert event["case_id"] == "CASE_001"
            assert event["tool_name"] in permissions[event["actor"]]
    item_ref = next(
        response["evidence_ref"] for response in gateway.responses if response["domain"] == "item"
    )
    assert item_ref not in output["claim_assessments"][0]["evidence_refs"]
    assert {event["event_type"] for event in trace.events} >= {
        "task_assigned",
        "tool_result_consumed",
        "handoff",
        "policy_decided",
        "verification_completed",
    }


def test_conflicting_evidence_reduces_confidence() -> None:
    evidence = [
        {"domain": "order", "data": {"order_status": "canceled"}},
        {"domain": "payment", "data": {"order_status": "paid"}},
    ]
    conflicts = _data_conflicts(evidence)
    assert conflicts == [
        {
            "field": "order_status",
            "sources": ["order", "payment"],
            "selected_source": "order",
            "resolution_code": "AUTHORITATIVE_DOMAIN_SELECTED",
        }
    ]
    assert _confidence(expected_count=2, evidence=evidence, conflicts=conflicts) == 0.8


@pytest.mark.parametrize(
    ("evidence", "issue"),
    [
        ([{"domain": "order", "data": {"order_status": "canceled"}}], "canceled_order_paid"),
        (
            [{"domain": "order", "data": {"order_status": "unavailable"}}],
            "unavailable_order_paid",
        ),
        (
            [
                {
                    "domain": "shipment",
                    "data": {"events": [{"event_type": "delivered_late", "actor": "seller"}]},
                }
            ],
            "late_delivery_seller",
        ),
        (
            [
                {
                    "domain": "shipment",
                    "data": {
                        "events": [
                            {"event_type": "delivered_late", "actor": "logistics_provider"}
                        ]
                    },
                }
            ],
            "late_delivery_logistics",
        ),
        (
            [
                {
                    "domain": "payment",
                    "data": {"events": [{"event_type": "reconciliation_mismatch"}]},
                }
            ],
            "payment_mismatch",
        ),
        (
            [
                {
                    "domain": "payment",
                    "data": [
                        {"payment_sequential": "1", "payment_type": "card", "payment_value": "64"},
                        {"payment_sequential": "1", "payment_type": "card", "payment_value": "64"},
                    ],
                }
            ],
            "duplicate_charge",
        ),
        (
            [
                {
                    "domain": "payment",
                    "data": [
                        {
                            "payment_sequential": "1",
                            "payment_type": "card",
                            "payment_value": "44.5",
                        },
                        {
                            "payment_sequential": "2",
                            "payment_type": "voucher",
                            "payment_value": "44.5",
                        },
                    ],
                }
            ],
            "valid_split_payment",
        ),
        (
            [
                {
                    "domain": "refund",
                    "data": {
                        "events": [{"event_type": "refund_requested", "status": "pending"}]
                    },
                }
            ],
            "refund_pending",
        ),
        (
            [
                {
                    "domain": "refund",
                    "data": {
                        "events": [{"event_type": "refund_requested", "status": "failed"}]
                    },
                }
            ],
            "refund_failed",
        ),
        ([{"domain": "order", "data": {"order_status": "delivered"}}], "unsupported_claim"),
    ],
)
def test_observed_issue_uses_authoritative_business_signals(
    evidence: list[dict], issue: str
) -> None:
    assert _observed_issue(evidence) == issue


def test_trace_lifecycle_must_be_complete_and_ordered() -> None:
    _validate_lifecycle("CASE_001", list(REQUIRED_LIFECYCLE))
    wrong_order = list(REQUIRED_LIFECYCLE)
    wrong_order[2], wrong_order[3] = wrong_order[3], wrong_order[2]
    with pytest.raises(ValueError, match="missing or misorders handoff"):
        _validate_lifecycle("CASE_001", wrong_order)
