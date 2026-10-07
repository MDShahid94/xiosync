"""Tests for the auto-trace pipeline: TraceCollector, PageProxy, DAGGraphBuilder.

Validates:
  - TraceCollector: record, step stack, auto-intent, summary, convenience methods
  - PageProxy: click/fill/goto interception, passthrough, capture failure
  - DAGGraphBuilder: trace → DAG conversion, intent dedup, terminal node, volatility
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

from xiosync.subsystems.xioflow.ingestion.trace_collector import (
    TraceAction,
    TraceCollector,
)

# ── TraceCollector ────────────────────────────────────────────────────────────


class TestTraceCollector:
    """Tests for TraceCollector action stream builder."""

    def test_empty_collector(self):
        tc = TraceCollector(org_id="org1", domain="example.com")
        assert tc.actions == []
        assert tc.current_step == "root"

    def test_record_action(self):
        tc = TraceCollector(org_id="org1", domain="example.com")
        tc.record(TraceAction(action_type="click"))
        assert len(tc.actions) == 1
        assert tc.actions[0].action_type == "click"
        assert tc.actions[0].step_name == "root"

    def test_step_stack(self):
        tc = TraceCollector(org_id="org1", domain="example.com")
        tc.enter_step("login")
        assert tc.current_step == "login"
        tc.record(TraceAction(action_type="fill"))
        assert tc.actions[0].step_name == "login"
        tc.exit_step("login")
        assert tc.current_step == "root"

    def test_nested_steps(self):
        tc = TraceCollector(org_id="org1", domain="example.com")
        tc.enter_step("outer")
        tc.enter_step("inner")
        assert tc.current_step == "inner"
        tc.record(TraceAction(action_type="click"))
        assert tc.actions[0].step_name == "inner"
        tc.exit_step("inner")
        assert tc.current_step == "outer"
        tc.exit_step("outer")
        assert tc.current_step == "root"

    def test_auto_intent_generation(self):
        tc = TraceCollector(org_id="org1", domain="example.com")
        tc.enter_step("phase1")
        tc.record(TraceAction(action_type="click"))
        assert tc.actions[0].intent == "phase1__click_0"

    def test_custom_intent_preserved(self):
        tc = TraceCollector(org_id="org1", domain="example.com")
        tc.record(TraceAction(action_type="click", intent="my_intent"))
        assert tc.actions[0].intent == "my_intent"

    def test_record_browser_action(self):
        tc = TraceCollector(org_id="org1", domain="example.com")
        tc.record_browser_action(
            action_type="click",
            place_value={"selector": "#btn"},
            url="https://example.com/page",
        )
        assert tc.actions[0].category == "browser"
        assert tc.actions[0].domain == "example.com"
        assert tc.actions[0].place_value == {"selector": "#btn"}

    def test_record_non_browser_action(self):
        tc = TraceCollector(org_id="org1", domain="example.com")
        tc.record_non_browser_action(
            action_type="http_request",
            action_params={"url": "/api/login", "method": "POST"},
        )
        assert tc.actions[0].category == "http"

    def test_to_summary(self):
        tc = TraceCollector(org_id="org1", domain="test.com", run_id="run1")
        tc.enter_step("step1")
        tc.record(TraceAction(action_type="click", category="browser"))
        tc.record(TraceAction(action_type="http_request", category="http"))
        tc.exit_step("step1")
        summary = tc.to_summary()
        assert summary["total_actions"] == 2
        assert summary["browser_actions"] == 1
        assert summary["non_browser_actions"] == 1
        assert "step1" in summary["steps"]
        assert summary["domain"] == "test.com"
        assert summary["run_id"] == "run1"


# ── PageProxy ─────────────────────────────────────────────────────────────────


class TestPageProxy:
    """Tests for PageProxy transparent Page wrapper."""

    def _run(self, coro):
        return asyncio.run(coro)

    def _make_proxy(self, dom_result=None):
        from xiosync.subsystems.xioflow.ingestion.page_proxy import PageProxy

        mock_page = MagicMock()
        mock_page.url = "https://example.com/test"
        mock_page.click = AsyncMock()
        mock_page.fill = AsyncMock()
        mock_page.type = AsyncMock()
        mock_page.goto = AsyncMock()
        mock_page.press = AsyncMock()
        mock_page.hover = AsyncMock()
        mock_page.wait_for_selector = AsyncMock()
        mock_page.wait_for_load_state = AsyncMock()
        mock_page.evaluate = AsyncMock()
        mock_page.text_content = AsyncMock(return_value="extracted text")

        trace = TraceCollector(org_id="org1", domain="example.com")

        dom = MagicMock()
        dom.inspect_target = AsyncMock(
            return_value=dom_result
            or {
                "place_value": {"selector": "#btn", "test_id": "submit"},
                "face_value": {"tag": "button", "text": "Submit"},
            }
        )

        proxy = PageProxy(mock_page, trace, dom)
        return proxy, mock_page, trace, dom

    def test_click_intercepts_and_records(self):
        proxy, page, trace, dom = self._make_proxy()
        self._run(proxy.click("#submit"))
        page.click.assert_called_once_with("#submit")
        assert len(trace.actions) == 1
        assert trace.actions[0].action_type == "click"
        assert trace.actions[0].place_value == {"selector": "#btn", "test_id": "submit"}

    def test_fill_intercepts_and_records(self):
        proxy, page, trace, dom = self._make_proxy()
        self._run(proxy.fill("#email", "test@test.com"))
        page.fill.assert_called_once_with("#email", "test@test.com")
        assert trace.actions[0].action_type == "fill"
        assert trace.actions[0].action_params["text"] == "test@test.com"

    def test_goto_records_navigate(self):
        proxy, page, trace, dom = self._make_proxy()
        self._run(proxy.goto("https://example.com"))
        page.goto.assert_called_once_with("https://example.com")
        assert trace.actions[0].action_type == "navigate"
        assert trace.actions[0].action_params["url"] == "https://example.com"

    def test_press_records_with_key(self):
        proxy, page, trace, dom = self._make_proxy()
        self._run(proxy.press("#input", "Enter"))
        page.press.assert_called_once_with("#input", "Enter")
        assert trace.actions[0].action_type == "press"
        assert trace.actions[0].action_params["key"] == "Enter"

    def test_text_content_records_extract(self):
        proxy, page, trace, dom = self._make_proxy()
        result = self._run(proxy.text_content("#result"))
        assert result == "extracted text"
        assert trace.actions[0].action_type == "extract_data"

    def test_evaluate_passthrough_no_record(self):
        proxy, page, trace, dom = self._make_proxy()
        self._run(proxy.evaluate("1 + 1"))
        page.evaluate.assert_called_once_with("1 + 1")
        assert len(trace.actions) == 0  # evaluate is not traced

    def test_getattr_passthrough(self):
        proxy, page, trace, dom = self._make_proxy()
        page.title = AsyncMock(return_value="Test Page")
        result = self._run(proxy.title())
        assert result == "Test Page"

    def test_url_property(self):
        proxy, page, trace, dom = self._make_proxy()
        assert proxy.url == "https://example.com/test"

    def test_capture_failure_still_records(self):
        proxy, page, trace, dom = self._make_proxy()
        dom.inspect_target = AsyncMock(side_effect=Exception("DOM error"))
        self._run(proxy.click("#btn"))
        # Action still recorded, just without locator data
        assert len(trace.actions) == 1
        assert trace.actions[0].place_value is None

    def test_no_dom_inspector(self):
        from xiosync.subsystems.xioflow.ingestion.page_proxy import PageProxy

        mock_page = MagicMock()
        mock_page.url = "https://example.com"
        mock_page.click = AsyncMock()
        trace = TraceCollector(org_id="org1", domain="example.com")
        proxy = PageProxy(mock_page, trace, dom_inspector=None)
        self._run(proxy.click("#btn"))
        assert len(trace.actions) == 1
        assert trace.actions[0].place_value is None


# ── DAGGraphBuilder ───────────────────────────────────────────────────────────


class TestDAGGraphBuilder:
    """Tests for DAGGraphBuilder trace → DAG conversion."""

    def test_empty_trace(self):
        from xiosync.subsystems.xioflow.ingestion.dag_graph_builder import DAGGraphBuilder

        tc = TraceCollector(org_id="org1", domain="example.com")
        result = DAGGraphBuilder().build(tc, {})
        assert result["nodes"] == []
        assert result["dag_domain"] == "example.com"

    def test_single_action_trace(self):
        from xiosync.subsystems.xioflow.ingestion.dag_graph_builder import DAGGraphBuilder

        tc = TraceCollector(org_id="org1", domain="example.com")
        tc.record(TraceAction(action_type="click", place_value={"selector": "#btn"}))
        result = DAGGraphBuilder().build(tc, {"device_type": "desktop"})
        # 1 action + 1 terminal "done" node
        assert len(result["nodes"]) == 2
        assert result["nodes"][0]["action_type"] == "click"
        assert result["nodes"][1]["action_type"] == "done"
        assert result["dag_root_intent"] == result["nodes"][0]["intent"]

    def test_multi_action_chain(self):
        from xiosync.subsystems.xioflow.ingestion.dag_graph_builder import DAGGraphBuilder

        tc = TraceCollector(org_id="org1", domain="example.com")
        tc.enter_step("login")
        tc.record(TraceAction(action_type="navigate", action_params={"url": "https://example.com"}))
        tc.record(TraceAction(action_type="fill", action_params={"text": "user@test.com"}))
        tc.record(TraceAction(action_type="click"))
        tc.exit_step("login")
        result = DAGGraphBuilder().build(tc, {})
        # 3 actions + 1 done
        assert len(result["nodes"]) == 4
        # Chain: node0 → node1 → node2 → done
        assert result["nodes"][0]["action_params"]["next_intents"] == [result["nodes"][1]["intent"]]
        assert result["nodes"][2]["action_params"]["next_intents"] == [result["nodes"][3]["intent"]]

    def test_previous_intent_linkage(self):
        from xiosync.subsystems.xioflow.ingestion.dag_graph_builder import DAGGraphBuilder

        tc = TraceCollector(org_id="org1", domain="example.com")
        tc.record(TraceAction(action_type="navigate"))
        tc.record(TraceAction(action_type="click"))
        result = DAGGraphBuilder().build(tc, {})
        assert result["nodes"][0]["previous_intent"] is None
        assert result["nodes"][1]["previous_intent"] == result["nodes"][0]["intent"]

    def test_recording_method_auto_trace(self):
        from xiosync.subsystems.xioflow.ingestion.dag_graph_builder import DAGGraphBuilder

        tc = TraceCollector(org_id="org1", domain="example.com")
        tc.record(TraceAction(action_type="click"))
        result = DAGGraphBuilder().build(tc, {})
        for node in result["nodes"]:
            assert node.get("recording_method") == "auto_trace"

    def test_control_actions_filtered(self):
        from xiosync.subsystems.xioflow.ingestion.dag_graph_builder import DAGGraphBuilder

        tc = TraceCollector(org_id="org1", domain="example.com")
        tc.record(TraceAction(action_type="wait_for_selector"))
        tc.record(TraceAction(action_type="click"))
        tc.record(TraceAction(action_type="wait_for_load_state"))
        result = DAGGraphBuilder().build(tc, {})
        # Only click + done should be present
        assert len(result["nodes"]) == 2
        assert result["nodes"][0]["action_type"] == "click"

    def test_action_type_mapping(self):
        from xiosync.subsystems.xioflow.ingestion.dag_graph_builder import DAGGraphBuilder

        tc = TraceCollector(org_id="org1", domain="example.com")
        tc.record(TraceAction(action_type="press"))
        tc.record(TraceAction(action_type="hover"))
        result = DAGGraphBuilder().build(tc, {})
        assert result["nodes"][0]["action_type"] == "fill"  # press → fill
        assert result["nodes"][1]["action_type"] == "click"  # hover → click

    def test_volatility_classification(self):
        from xiosync.subsystems.xioflow.ingestion.dag_graph_builder import DAGGraphBuilder

        tc = TraceCollector(org_id="org1", domain="example.com")
        tc.record(TraceAction(action_type="click", category="browser"))
        tc.record(TraceAction(action_type="navigate", category="browser"))
        result = DAGGraphBuilder().build(tc, {})
        assert result["nodes"][0].get("volatility_type") == "dynamic"  # click
        assert result["nodes"][1].get("volatility_type") == "static"  # navigate

    def test_metadata_included(self):
        from xiosync.subsystems.xioflow.ingestion.dag_graph_builder import DAGGraphBuilder

        tc = TraceCollector(org_id="org1", domain="example.com", run_id="run1")
        tc.record(TraceAction(action_type="click"))
        result = DAGGraphBuilder().build(tc, {}, template_name="google-signin")
        assert result["metadata"]["source_template"] == "google-signin"
        assert result["metadata"]["recording_method"] == "auto_trace"
        assert "trace_summary" in result["metadata"]

    def test_intent_deduplication(self):
        from xiosync.subsystems.xioflow.ingestion.dag_graph_builder import DAGGraphBuilder

        tc = TraceCollector(org_id="org1", domain="example.com")
        # Force same intent on two actions
        tc.record(TraceAction(action_type="click", intent="same_intent"))
        tc.record(TraceAction(action_type="click", intent="same_intent"))
        result = DAGGraphBuilder().build(tc, {})
        intents = [n["intent"] for n in result["nodes"] if n["action_type"] != "done"]
        assert len(set(intents)) == 2  # deduplicated
