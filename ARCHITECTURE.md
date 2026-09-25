# L3A Architecture Record

## 1. System overview

```text
Input → Coordinator ─┬→ Order/item ──┐
                     ├→ Payment ─────┼→ Policy → Verifier → Output
                     └→ Shipment ────┘
                           │              │         │
                           └──── MCP ─────┴─────────┴→ Trace
```

`solve_case` là async state machine thuần Python. Coordinator lấy hypothesis từ claim chỉ để
chọn specialist/tool; claim không trở thành ground truth. Chỉ MCP evidence đã validate mới được
handoff cho Policy. Thiếu evidence bắt buộc luôn cho kết quả `insufficient_evidence`.
`contracts/scoring/scoring-policy-v2.json` là scoring/hard-gate policy, không phải bảng quyền lợi
nghiệp vụ; Policy Agent lấy quy tắc trọng tài theo case từ MCP `get_policy`, còn Verifier áp dụng
các tiêu chí provenance, consistency, calibration và workflow của scoring policy.

## 2. Agent ownership

| Actor | Input | Trách nhiệm | Output/handoff |
| --- | --- | --- | --- |
| Coordinator | case input | Route theo claim, fan-out/fan-in; không gọi MCP | task assignment |
| Order/item | `case_id`, `order_id` | `get_order`, `get_order_items`, khi cần `get_sellers` | evidence handoff |
| Payment | `case_id`, `order_id` | `get_order_payments`, `get_payment_timeline`, `get_refund_timeline` | evidence handoff |
| Shipment | `case_id`, `order_id` | `get_shipment_summary` chỉ cho claim giao hàng | evidence handoff |
| Policy | evidence, `policy_version` | `get_policy`; quyết định issue/status/action | candidate output |
| Verifier | candidate output | Không gọi tool; kiểm evidence linkage và financial total | validated output |

## 3. A2A protocol

Message nội bộ gồm `case_id`, actor đích, tool arguments và evidence envelope nguyên bản. Mọi
call và trace đều correlation bằng cùng `case_id`. Specialist handoff một lần sau khi các call
kết thúc; Policy handoff một lần sang Verifier, nên đồ thị không có cạnh quay lại và không thể
lặp vô hạn. Mỗi call timeout sau 30 giây, tối đa hai lần thử. Trace chỉ chứa lifecycle,
decision code và evidence ref quan sát được; không chứa prompt hay suy luận riêng.

## 4. Evidence lifecycle

`EvidenceGateway.call` validate envelope bằng `mcp-evidence-response-v1` trước khi trả về.
Specialist emit `tool_result_consumed` ngay khi dùng response. Evidence chỉ sống trong stack của
một lần `solve_case`; output và claim chỉ tham chiếu tập ref đã thu trong case đó. Gateway luôn
gửi `case_id`, vì vậy không có cache hay tái sử dụng evidence chéo case. Workflow kiểm domain
trả về khớp tool và giữ nguyên `evidence_ref`; claim chỉ nhận refs thuộc các domain liên quan
(ví dụ payment claim không trích dẫn item evidence).

## 5. Failure policy

| Failure | Retry? | Fallback | Trace event/code |
| --- | --- | --- | --- |
| MCP timeout/lỗi tạm thời | Một lần, cùng arguments | `insufficient_evidence` | handoff / `EVIDENCE_INCOMPLETE` |
| Not found | Không | `insufficient_evidence` | handoff / `EVIDENCE_INCOMPLETE` |
| Source conflict | Không | `needs_investigation`; không tự chọn nguồn | `policy_decided` |
| Invalid envelope/result | Không | fail validation; không consume ref | không emit consume |

Read-only MCP calls retry với cùng `case_id` và arguments nên idempotent. Không có evidence thì
không tạo ref, entity, số tiền hay kết luận giả.

## 6. Verification invariants

Verifier buộc claim refs là tập con của output refs; tổng `refund_lines` bằng
`recommended_refund_brl`; seller chịu trách nhiệm chỉ nhận ID lấy từ evidence; thiếu evidence
giới hạn confidence dưới 0.5 và không phát sinh refund. Confidence bắt đầu từ coverage evidence,
giảm theo warning/mâu thuẫn và không vượt 0.95. Verifier kiểm mapping issue–responsible party,
status–refund–action và giới hạn confidence khi có conflict. Sau khi hàm trả về, CLI validate
toàn bộ output bằng `l3a-output-v2` trước khi ghi. Submission validator còn buộc đúng thứ tự
`case_received → task_assigned → tool_result_consumed → handoff → policy_decided →
verification_completed → case_finalized` cho từng case.

## 7. Reproducibility

Runtime Python >=3.11, dependency ranges nằm trong `pyproject.toml`. Ba specialist chạy đồng
thời; call trong mỗi specialist cũng chạy đồng thời. Workflow deterministic, không dùng model,
random seed hay shared mutable state. Chạy `day09 run`, kiểm tra bằng `pytest` và
`day09 validate`. Credential chỉ đọc từ `.env`, không ghi vào output/trace.
