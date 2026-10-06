"""XIOVIEW — Universal Browser Session Observation Subsystem.

Provides live visual observation, action logging, and remote control for
any active browser session, regardless of what workflow is running.

Observation modes (selectable per-session via ?mode=...):
  screenshot        — JPEG frame polling (universal, CSP-immune, canvas-safe)
  cdp_screencast    — Chrome DevTools Protocol screencast (Chromium, 24fps, GPU)
  dom_stream        — rrweb DOM serialization (low-bandwidth, semantic, interactive)
  cdp_dom_snapshot  — CDP DOMSnapshot.captureSnapshot (structured DOM, no JS injection)
  dom_overlay       — Hybrid screencast + semantic DOM element hitboxes (element-aware)

Interactions are dispatched to remote Chrome via raw CDP Input.dispatch*
commands for isTrusted=true events that pass bot detection. Coordinate
scaling handles mismatches between stream dimensions and actual viewport.

All modes share the same WebSocket channel, which additionally carries
action log events from dag_executor — giving operators a synchronized
live view AND structured audit trail in one connection.

Module structure:
  protocol.py        — shared types, constants, and interfaces
  registry.py        — session tracking and frame queuing
  session_manager.py — browser session lifecycle and CDP session cache
  control.py         — input dispatch, coordinate scaling, run guards
  screencast.py      — CDP screencast engine and rrweb injector
  viewer.py          — HTML viewer template rendering
  api/routes.py      — FastAPI WebSocket and REST endpoints
  modes/             — pluggable observation strategies
"""
