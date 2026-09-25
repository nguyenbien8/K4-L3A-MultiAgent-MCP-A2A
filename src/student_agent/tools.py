from typing import Any, List
from langchain_core.tools import tool

def create_mcp_tools(gateway, trace, case_id: str, actor_name: str, allowed_tools: List[str]):
    """Creates a list of LangChain tools that securely call the MCP Gateway."""
    tools = []
    
    if "get_order" in allowed_tools:
        @tool
        async def get_order(order_id: str) -> dict[str, Any]:
            """Retrieve order details including status and timestamps."""
            evidence = await gateway.call("get_order", case_id=case_id, order_id=order_id)
            trace.emit(
                case_id=case_id, event_type="tool_result_consumed",
                actor=actor_name, tool_name="get_order", evidence_refs=[evidence["evidence_ref"]]
            )
            return evidence["data"]
        tools.append(get_order)
        
    if "get_order_items" in allowed_tools:
        @tool
        async def get_order_items(order_id: str) -> dict[str, Any]:
            """Retrieve all items belonging to a specific order."""
            evidence = await gateway.call("get_order_items", case_id=case_id, order_id=order_id)
            trace.emit(
                case_id=case_id, event_type="tool_result_consumed",
                actor=actor_name, tool_name="get_order_items", evidence_refs=[evidence["evidence_ref"]]
            )
            return evidence["data"]
        tools.append(get_order_items)

    if "get_order_payments" in allowed_tools:
        @tool
        async def get_order_payments(order_id: str) -> dict[str, Any]:
            """Retrieve payment information for an order."""
            evidence = await gateway.call("get_order_payments", case_id=case_id, order_id=order_id)
            trace.emit(
                case_id=case_id, event_type="tool_result_consumed",
                actor=actor_name, tool_name="get_order_payments", evidence_refs=[evidence["evidence_ref"]]
            )
            return evidence["data"]
        tools.append(get_order_payments)

    if "get_payment_timeline" in allowed_tools:
        @tool
        async def get_payment_timeline(payment_id: str) -> dict[str, Any]:
            """Retrieve timeline events for a specific payment."""
            evidence = await gateway.call("get_payment_timeline", case_id=case_id, payment_id=payment_id)
            trace.emit(
                case_id=case_id, event_type="tool_result_consumed",
                actor=actor_name, tool_name="get_payment_timeline", evidence_refs=[evidence["evidence_ref"]]
            )
            return evidence["data"]
        tools.append(get_payment_timeline)
        
    if "get_refund_timeline" in allowed_tools:
        @tool
        async def get_refund_timeline(payment_id: str) -> dict[str, Any]:
            """Retrieve refund timeline for a specific payment."""
            evidence = await gateway.call("get_refund_timeline", case_id=case_id, payment_id=payment_id)
            trace.emit(
                case_id=case_id, event_type="tool_result_consumed",
                actor=actor_name, tool_name="get_refund_timeline", evidence_refs=[evidence["evidence_ref"]]
            )
            return evidence["data"]
        tools.append(get_refund_timeline)
        
    if "get_shipment_summary" in allowed_tools:
        @tool
        async def get_shipment_summary(order_id: str) -> dict[str, Any]:
            """Retrieve the shipment status and logistics details for an order."""
            evidence = await gateway.call("get_shipment_summary", case_id=case_id, order_id=order_id)
            trace.emit(
                case_id=case_id, event_type="tool_result_consumed",
                actor=actor_name, tool_name="get_shipment_summary", evidence_refs=[evidence["evidence_ref"]]
            )
            return evidence["data"]
        tools.append(get_shipment_summary)
        
    if "get_policy" in allowed_tools:
        @tool
        async def get_policy(policy_id: str) -> dict[str, Any]:
            """Retrieve platform or seller policies regarding returns, refunds, etc."""
            evidence = await gateway.call("get_policy", case_id=case_id, policy_id=policy_id)
            trace.emit(
                case_id=case_id, event_type="tool_result_consumed",
                actor=actor_name, tool_name="get_policy", evidence_refs=[evidence["evidence_ref"]]
            )
            return evidence["data"]
        tools.append(get_policy)

    return tools
