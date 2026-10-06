from __future__ import annotations

import pytest

from xiosync.subsystems.xioview.modes.dom_overlay import _parse_attributes, _collect_interactive

def test_parse_attributes_flat_list():
    attrs = ["id", "btn1", "class", "primary", "data-x", "123"]
    res = _parse_attributes(attrs)
    assert res == {"id": "btn1", "class": "primary", "data-x": "123"}

def test_parse_attributes_empty():
    res = _parse_attributes([])
    assert res == {}

def test_collect_interactive_finds_buttons():
    node = {
        "nodeType": 1,
        "nodeName": "button",
        "nodeId": 10,
        "attributes": ["id", "submit"]
    }
    result = []
    _collect_interactive(node, result, 10)
    assert len(result) == 1
    assert result[0]["nodeId"] == 10
    assert result[0]["tag"] == "button"
    assert result[0]["id"] == "submit"

def test_collect_interactive_finds_links():
    node = {
        "nodeType": 1,
        "nodeName": "a",
        "nodeId": 11,
    }
    result = []
    _collect_interactive(node, result, 10)
    assert len(result) == 1
    assert result[0]["tag"] == "a"

def test_collect_interactive_finds_role_button():
    node = {
        "nodeType": 1,
        "nodeName": "div",
        "nodeId": 12,
        "attributes": ["role", "button"]
    }
    result = []
    _collect_interactive(node, result, 10)
    assert len(result) == 1
    assert result[0]["role"] == "button"
    assert result[0]["interactive"] is True

def test_collect_interactive_respects_max_count():
    child1 = {"nodeType": 1, "nodeName": "button", "nodeId": 2}
    child2 = {"nodeType": 1, "nodeName": "button", "nodeId": 3}
    node = {
        "nodeType": 1,
        "nodeName": "button",
        "nodeId": 1,
        "children": [child1, child2]
    }
    result = []
    _collect_interactive(node, result, 2)
    assert len(result) == 2

def test_collect_interactive_traverses_shadow_dom():
    shadow_node = {"nodeType": 1, "nodeName": "input", "nodeId": 2}
    node = {
        "nodeType": 1,
        "nodeName": "div",
        "nodeId": 1,
        "shadowRoots": [
            {"nodeType": 1, "nodeName": "#document-fragment", "children": [shadow_node]}
        ]
    }
    result = []
    _collect_interactive(node, result, 10)
    assert len(result) == 1
    assert result[0]["nodeId"] == 2
    assert result[0]["tag"] == "input"
