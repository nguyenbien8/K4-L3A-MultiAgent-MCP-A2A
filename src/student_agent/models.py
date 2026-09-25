from pydantic import BaseModel, Field
from typing import List, Optional, Literal

class Assessment(BaseModel):
    primary_issue: Literal[
        "canceled_order_paid", "unavailable_order_paid", "late_delivery_seller",
        "late_delivery_logistics", "valid_split_payment", "payment_mismatch",
        "duplicate_charge", "refund_pending", "refund_failed",
        "unsupported_claim", "insufficient_evidence"
    ]
    case_status: Literal["action_required", "no_action", "needs_investigation"]
    confidence: float = Field(ge=0, le=1)

class AffectedEntities(BaseModel):
    order_ids: List[str] = Field(max_length=20, default_factory=list)
    item_ids: List[str] = Field(max_length=20, default_factory=list)
    seller_ids: List[str] = Field(max_length=20, default_factory=list)
    payment_references: List[str] = Field(max_length=20, default_factory=list)
    shipment_ids: List[str] = Field(max_length=20, default_factory=list)

class ClaimAssessment(BaseModel):
    claim_id: str = Field(min_length=1, max_length=64)
    verdict: Literal["supported", "unsupported", "partially_supported", "insufficient_evidence"]
    confidence: float = Field(ge=0, le=1)
    evidence_refs: List[str] = Field(max_length=30)

class RankedCause(BaseModel):
    cause_code: str = Field(pattern=r"^[A-Z][A-Z0-9_]{2,79}$")
    rank: int = Field(ge=1, le=5)

class ResponsibleParty(BaseModel):
    party_type: Literal["seller", "platform", "logistics_provider", "payment_provider", "customer", "unknown"]
    party_id: Optional[str] = Field(max_length=128, default=None)

class RootCauseAnalysis(BaseModel):
    ranked_causes: List[RankedCause] = Field(max_length=5)
    responsible_parties: List[ResponsibleParty] = Field(max_length=5)

class DataConflict(BaseModel):
    field: str = Field(min_length=1, max_length=100)
    sources: List[str] = Field(min_length=2, max_length=5)
    selected_source: Optional[str] = Field(max_length=80, default=None)
    resolution_code: str = Field(min_length=1, max_length=80)

class RefundLine(BaseModel):
    reason_code: str = Field(min_length=1, max_length=80)
    amount_brl: float = Field(ge=0)
    entity_id: Optional[str] = Field(max_length=128, default=None)

class FinancialResolution(BaseModel):
    currency: Literal["BRL"] = "BRL"
    recommended_refund_brl: float = Field(ge=0)
    refund_lines: List[RefundLine] = Field(max_length=10, default_factory=list)

class CaseOutput(BaseModel):
    schema_version: Literal["day09-l3a-output-v2"] = "day09-l3a-output-v2"
    case_id: str = Field(pattern=r"^[A-Z0-9][A-Z0-9_-]{2,63}$")
    assessment: Assessment
    affected_entities: AffectedEntities
    claim_assessments: Optional[List[ClaimAssessment]] = Field(max_length=5, default_factory=list)
    root_cause_analysis: RootCauseAnalysis
    evidence_refs: List[str] = Field(max_length=30, default_factory=list)
    data_conflicts: List[DataConflict] = Field(max_length=5, default_factory=list)  # required by schema, must be array
    financial_resolution: FinancialResolution
    resolution_actions: List[str] = Field(max_length=8, default_factory=list)  # required by schema, must be array
