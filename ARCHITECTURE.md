# L3A Architecture Record

Team phải cập nhật tài liệu này cùng source. Mục tiêu là mô tả quyết định có thể kiểm chứng, không ghi prompt bí mật hoặc chain-of-thought.

## 1. System overview

Vẽ hoặc mô tả luồng từ `inputs/<case_id>.json` đến MCP calls, specialist agents, verifier, output và trace.

```text
Input → Coordinator (LangGraph Router)
             ├─→ Order/Item Agent  ─→ MCP (get_order, get_order_items)
             ├─→ Payment Agent     ─→ MCP (get_order_payments, get_payment_timeline...)
             ├─→ Shipment Agent    ─→ MCP (get_shipment_summary)
             └─→ Policy Agent      ─→ MCP (get_policy, get_refund_timeline)
                         │
                         ▼
                  Verifier Agent
                         │
                         ▼
             Output (Pydantic models) & Trace
```

## 2. Agent ownership

| Actor | Input | Trách nhiệm | Output/handoff |
| --- | --- | --- | --- |
| Coordinator | `case_id`, user complaints | Phân tích khiếu nại, quyết định xem cần gọi những specialist agent nào. | Handoff tới các specialist agents (Parallel/Conditional). |
| Order/item | `case_id`, `order_id` | Lấy dữ liệu về order và item, kiểm tra trạng thái đơn hàng. | Trả về thông tin đơn hàng và evidence cho Verifier. |
| Payment | `case_id`, `payment_id` | Kiểm tra giao dịch, lịch sử thanh toán, tình trạng hoàn tiền. | Trả về thông tin thanh toán và evidence cho Verifier. |
| Shipment | `case_id`, `shipment_id` | Theo dõi trạng thái giao hàng, kiểm tra trễ hạn hoặc thất lạc. | Trả về trạng thái giao hàng và evidence cho Verifier. |
| Policy | `case_id`, `policy_id` | Kiểm tra các chính sách đổi trả, hoàn tiền của sàn/seller. | Trả về chính sách tương ứng và evidence cho Verifier. |
| Verifier | Dữ liệu từ các specialists | Kiểm tra chéo (Data conflicts), tổng hợp bằng chứng, map vào Schema. | Xuất ra JSON Output tuân thủ `l3a-output-v2` và Trace log. |

Nêu rõ actor nào được quyền gọi tool nào. Tránh cho mọi agent quyền truy vấn tất cả tool nếu không cần thiết.

## 3. A2A protocol

Mô tả message envelope, correlation theo `case_id`, điều kiện handoff, timeout và cách tránh vòng lặp. Chỉ trace sự kiện/decision code quan sát được; không trace nội dung suy luận riêng.

## 4. Evidence lifecycle

Mô tả cách validate MCP response, lưu `evidence_ref`, map evidence vào claim/output và emit `tool_result_consumed`. Evidence không được tái sử dụng giữa các case.

## 5. Failure policy

| Failure | Retry? | Fallback | Trace event/code |
| --- | --- | --- | --- |
| MCP timeout | TODO | TODO | TODO |
| Not found | TODO | TODO | TODO |
| Source conflict | TODO | TODO | TODO |
| Invalid specialist result | TODO | TODO | TODO |

Retry phải có giới hạn và idempotent. Không chuyển missing evidence thành dữ liệu phỏng đoán.

## 6. Verification invariants

Liệt kê kiểm tra trước finalize: schema, entity scope, evidence ownership, claim linkage, money totals, responsibility/action consistency và confidence bounds.

## 7. Reproducibility

Ghi model/config, dependency pinning, concurrency limit, random seed (nếu có), lệnh chạy và các giới hạn tài nguyên. Không ghi API key.
