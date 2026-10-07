from __future__ import annotations

from xiosync.subsystems.xioview.modes.cdp_dom_snapshot import _strip_script_nodes


def test_strip_script_nodes_removes_content():
    snapshot = {
        "strings": ["", "html", "head", "script", "var x=1;"],
        "documents": [{"nodes": {"nodeName": [1, 2, 3], "nodeValue": [-1, -1, 4]}}],
    }
    _strip_script_nodes(snapshot)
    assert snapshot["strings"][4] == ""


def test_strip_script_nodes_preserves_non_script():
    snapshot = {
        "strings": ["", "div", "hello"],
        "documents": [{"nodes": {"nodeName": [1], "nodeValue": [2]}}],
    }
    _strip_script_nodes(snapshot)
    assert snapshot["strings"][2] == "hello"


def test_strip_script_nodes_handles_noscript():
    snapshot = {
        "strings": ["", "noscript", "hidden content"],
        "documents": [{"nodes": {"nodeName": [1], "nodeValue": [2]}}],
    }
    _strip_script_nodes(snapshot)
    assert snapshot["strings"][2] == ""


def test_strip_script_nodes_empty_snapshot():
    snapshot = {}
    _strip_script_nodes(snapshot)  # Should not crash
