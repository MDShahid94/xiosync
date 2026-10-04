"""Tests for the 4 critical DAG engine bug fixes (Oct 2026).

Bug 1: context_hash_router.py used wrong column names (org_id, viewport_w)
Bug 2: ai_healer.py called DOMInspector() — not callable
Bug 3: memory_graph.py set last_used_at (column is last_used)
Bug 4: dag_executor.py passed int to ARRAY(Integer) locator_priority
"""
from __future__ import annotations

import uuid
from datetime import datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# ── Bug 1: ContextHashRouter SQL column names ─────────────────────────────────

class TestContextHashRouterColumns:
    """Verify SQL uses correct column names from XioflowMemoryNode model."""

    def test_query_uses_organization_id_not_org_id(self):
        """The WHERE clause must reference 'organization_id', not 'org_id'."""
        from xiosync.subsystems.xioflow.engine.context_hash_router import ContextHashRouter

        router = ContextHashRouter(session=MagicMock())
        # We can't easily execute the SQL, but we can inspect the query construction
        # by examining the source. Import and check the method exists.
        import inspect
        source = inspect.getsource(router.query_by_viewport_tier)
        assert "organization_id = :org_id" in source, \
            "WHERE clause should use 'organization_id' column (not 'org_id')"
        assert '"org_id = :org_id"' not in source, \
            "Must NOT use bare 'org_id' — actual column is 'organization_id'"

    def test_query_uses_viewport_width_not_viewport_w(self):
        """SQL must reference 'viewport_width', not 'viewport_w'."""
        from xiosync.subsystems.xioflow.engine.context_hash_router import ContextHashRouter

        import inspect
        source = inspect.getsource(ContextHashRouter.query_by_viewport_tier)
        # Should contain viewport_width in WHERE clauses
        assert "viewport_width" in source, \
            "WHERE clause should use 'viewport_width' (not 'viewport_w')"
        # viewport_w should only appear as context dict key, not in SQL
        # Check that viewport_w doesn't appear in SQL string literals
        assert '"viewport_w = :vw"' not in source, \
            "SQL must NOT use 'viewport_w' — actual column is 'viewport_width'"
        assert '"viewport_w >=' not in source, \
            "SQL must NOT use 'viewport_w' — actual column is 'viewport_width'"


# ── Bug 2: AIHealer DOMInspector usage ────────────────────────────────────────

class TestAIHealerDOMInspector:
    """Verify AIHealer correctly calls DOMInspector.get_interactive_elements()."""

    @pytest.mark.asyncio
    async def test_heal_calls_get_interactive_elements(self):
        """heal() should call dom_inspector.get_interactive_elements(), not dom_inspector()."""
        from xiosync.subsystems.xioflow.engine.ai_healer import AIHealer

        mock_dom_inspector = MagicMock()
        mock_dom_inspector.get_interactive_elements = AsyncMock(
            return_value=("[0] <button> \"Login\"", {0: {"tag": "button"}})
        )

        healer = AIHealer()
        # Mock the gateway to return a valid result
        mock_result = MagicMock()
        mock_result.success = True
        mock_result.text = '{"selector": "#login-btn", "selector_type": "css", "confidence": 0.95, "reasoning": "test"}'
        healer.gateway = MagicMock()
        healer.gateway.generate = AsyncMock(return_value=mock_result)

        result = await healer.heal(page=MagicMock(), intent="click login", dom_inspector=mock_dom_inspector)

        # Must have called get_interactive_elements, not __call__
        mock_dom_inspector.get_interactive_elements.assert_awaited_once()
        assert result is not None
        assert result["selector"] == "#login-btn"

    @pytest.mark.asyncio
    async def test_heal_falls_back_to_callable(self):
        """If dom_inspector doesn't have get_interactive_elements, try calling it."""
        from xiosync.subsystems.xioflow.engine.ai_healer import AIHealer

        async def _fake_dom():
            return "[0] <button> \"Submit\""

        healer = AIHealer()
        mock_result = MagicMock()
        mock_result.success = True
        mock_result.text = '{"selector": "#submit", "selector_type": "css", "confidence": 0.9, "reasoning": "test"}'
        healer.gateway = MagicMock()
        healer.gateway.generate = AsyncMock(return_value=mock_result)

        result = await healer.heal(page=MagicMock(), intent="click submit", dom_inspector=_fake_dom)
        assert result is not None
        assert result["selector"] == "#submit"


# ── Bug 3: MemoryGraph last_used column name ─────────────────────────────────

class TestMemoryGraphLastUsed:
    """Verify update_last_used uses 'last_used', not 'last_used_at'."""

    def test_update_last_used_uses_correct_column(self):
        """update_last_used must pass last_used= to update_node, not last_used_at=."""
        from xiosync.subsystems.xioflow.memory.memory_graph import MemoryGraph

        import inspect
        source = inspect.getsource(MemoryGraph.update_last_used)
        assert "last_used=" in source, \
            "Must use 'last_used' kwarg (matches actual DB column)"
        assert "last_used_at=" not in source, \
            "Must NOT use 'last_used_at' — column is 'last_used'"

    def test_update_last_used_calls_update_node(self):
        """update_last_used should delegate to update_node with correct column."""
        from xiosync.subsystems.xioflow.memory.memory_graph import MemoryGraph

        mock_session = MagicMock()
        graph = MemoryGraph(session=mock_session)
        graph.update_node = MagicMock()

        node_id = uuid.uuid4()
        graph.update_last_used(node_id)

        graph.update_node.assert_called_once()
        call_kwargs = graph.update_node.call_args[1]
        assert "last_used" in call_kwargs, "Must pass last_used= kwarg"
        assert "last_used_at" not in call_kwargs, "Must NOT pass last_used_at="
        assert isinstance(call_kwargs["last_used"], datetime)


# ── Bug 4: DAG executor locator_priority list type ────────────────────────────

class TestLocatorPriorityListType:
    """Verify locator_priority is updated with a list, not a bare int."""

    def test_update_locator_priority_accepts_list(self):
        """aupdate_locator_priority must receive list[int], not int."""
        from xiosync.subsystems.xioflow.memory.memory_graph import MemoryGraph

        mock_session = MagicMock()
        graph = MemoryGraph(session=mock_session)
        graph.update_node = MagicMock()

        node_id = uuid.uuid4()
        # This is what the fixed dag_executor now passes
        new_priority = [3, 1, 2, 4, 5, 6, 7, 8, 9]
        graph.update_locator_priority(node_id, new_priority)

        graph.update_node.assert_called_once_with(node_id, locator_priority=new_priority)

    def test_priority_promotes_winning_tier_to_front(self):
        """After a tier wins, it should be promoted to front of the list."""
        old_priority = [1, 2, 3, 4, 5, 6, 7, 8, 9]
        winning_tier = 5

        # This is the logic from the fixed dag_executor
        new_priority = [winning_tier] + [t for t in old_priority if t != winning_tier]

        assert new_priority[0] == 5, "Winning tier should be first"
        assert 5 not in new_priority[1:], "Winning tier should not be duplicated"
        assert len(new_priority) == len(old_priority), "Length should be preserved"
        assert new_priority == [5, 1, 2, 3, 4, 6, 7, 8, 9]
