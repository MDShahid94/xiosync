"""XIOVIEW observation modes — pluggable capture strategies.

Each mode provides a different way to observe and stream a remote browser session:
  - cdp_dom_snapshot: Periodic DOM tree snapshots via CDP DOMSnapshot domain
  - dom_overlay:      Hybrid screencast + semantic DOM element hitboxes
"""
