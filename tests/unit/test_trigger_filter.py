"""Unit tests for trigger filter expressions and evaluation."""

from __future__ import annotations

from unittest.mock import MagicMock
import uuid

import pytest

from xiosync.domain.triggers import _evaluate_filter, evaluate_filter
from xiosync.subsystems.xioflow.domain.trigger_filter import (
    _evaluate_filter as xioflow_eval_filter,
    evaluate_filter as xioflow_evaluate_filter,
)
from xiosync.worker.event_router import evaluate_event_triggers


def test_evaluate_filter_none():
    assert _evaluate_filter(None, {"amount": 500}) is True
    assert evaluate_filter(None, {}) is True
    assert xioflow_eval_filter(None, {}) is True
    assert xioflow_evaluate_filter(None, {}) is True


def test_evaluate_filter_operators():
    payload = {
        "amount": 1500,
        "resource_type": "workflow",
        "tags": ["prod", "finance"],
        "customer": {
            "tier": "platinum",
            "score": 85,
        },
    }

    # eq
    assert _evaluate_filter({"field": "resource_type", "op": "eq", "value": "workflow"}, payload) is True
    assert _evaluate_filter({"field": "resource_type", "op": "eq", "value": "agent"}, payload) is False

    # ne
    assert _evaluate_filter({"field": "resource_type", "op": "ne", "value": "agent"}, payload) is True
    assert _evaluate_filter({"field": "resource_type", "op": "ne", "value": "workflow"}, payload) is False

    # gt / gte
    assert _evaluate_filter({"field": "amount", "op": "gt", "value": 1000}, payload) is True
    assert _evaluate_filter({"field": "amount", "op": "gt", "value": 1500}, payload) is False
    assert _evaluate_filter({"field": "amount", "op": "gte", "value": 1500}, payload) is True
    assert _evaluate_filter({"field": "amount", "op": "gte", "value": 2000}, payload) is False

    # lt / lte
    assert _evaluate_filter({"field": "amount", "op": "lt", "value": 2000}, payload) is True
    assert _evaluate_filter({"field": "amount", "op": "lt", "value": 1500}, payload) is False
    assert _evaluate_filter({"field": "amount", "op": "lte", "value": 1500}, payload) is True
    assert _evaluate_filter({"field": "amount", "op": "lte", "value": 1000}, payload) is False

    # in / contains
    assert _evaluate_filter({"field": "resource_type", "op": "in", "value": ["workflow", "script"]}, payload) is True
    assert _evaluate_filter({"field": "resource_type", "op": "in", "value": ["plugin"]}, payload) is False
    assert _evaluate_filter({"field": "tags", "op": "contains", "value": "prod"}, payload) is True
    assert _evaluate_filter({"field": "tags", "op": "contains", "value": "staging"}, payload) is False

    # exists
    assert _evaluate_filter({"field": "customer.tier", "op": "exists"}, payload) is True
    assert _evaluate_filter({"field": "customer.missing", "op": "exists"}, payload) is False


def test_evaluate_filter_nested_dot_notation():
    payload = {"data": {"nested": {"value": 42}}}
    assert _evaluate_filter({"field": "data.nested.value", "op": "eq", "value": 42}, payload) is True
    assert _evaluate_filter({"field": "data.nested.value", "op": "gt", "value": 40}, payload) is True
    assert _evaluate_filter({"field": "data.nested.other", "op": "eq", "value": 42}, payload) is False


def test_evaluate_filter_boolean_logic():
    payload = {"amount": 2500, "status": "active", "level": 3}

    # all (AND)
    expr_all_pass = {
        "all": [
            {"field": "amount", "op": "gt", "value": 1000},
            {"field": "status", "op": "eq", "value": "active"},
        ]
    }
    assert _evaluate_filter(expr_all_pass, payload) is True

    expr_all_fail = {
        "all": [
            {"field": "amount", "op": "gt", "value": 5000},
            {"field": "status", "op": "eq", "value": "active"},
        ]
    }
    assert _evaluate_filter(expr_all_fail, payload) is False

    # any (OR)
    expr_any_pass = {
        "any": [
            {"field": "amount", "op": "gt", "value": 5000},
            {"field": "status", "op": "eq", "value": "active"},
        ]
    }
    assert _evaluate_filter(expr_any_pass, payload) is True

    expr_any_fail = {
        "any": [
            {"field": "amount", "op": "gt", "value": 5000},
            {"field": "status", "op": "eq", "value": "inactive"},
        ]
    }
    assert _evaluate_filter(expr_any_fail, payload) is False


def test_event_router_filters_matches():
    session = MagicMock()
    org_id = uuid.uuid4()
    event_id = uuid.uuid4()
    trigger_id_pass = uuid.uuid4()
    trigger_id_fail = uuid.uuid4()

    row_pass = MagicMock()
    row_pass.event_id = event_id
    row_pass.event_type = "payment.received"
    row_pass.organization_id = org_id
    row_pass.payload = {"amount": 5000}
    row_pass.trigger_id = trigger_id_pass
    row_pass.template_id = None
    row_pass.context_defaults = {}
    row_pass.filter_expression = {"field": "amount", "op": "gt", "value": 1000}

    row_fail = MagicMock()
    row_fail.event_id = event_id
    row_fail.event_type = "payment.received"
    row_fail.organization_id = org_id
    row_fail.payload = {"amount": 500}
    row_fail.trigger_id = trigger_id_fail
    row_fail.template_id = None
    row_fail.context_defaults = {}
    row_fail.filter_expression = {"field": "amount", "op": "gt", "value": 1000}

    session.execute.return_value.fetchall.return_value = [row_pass, row_fail]

    created = evaluate_event_triggers(session)
    # Only row_pass should have fired!
    assert created == 1
    session.commit.assert_called_once()
