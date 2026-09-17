"""XIOVIEW — Universal Browser Session Observation Subsystem.

Provides live visual observation, action logging, and remote control for
any active browser session, regardless of what workflow is running.

Observation modes (selectable per-session via ?mode=...):
  screenshot      — JPEG frame polling (universal, CSP-immune, canvas-safe)
  cdp_screencast  — Chrome DevTools Protocol screencast (Chromium, 24fps, GPU)
  dom_stream      — rrweb DOM serialization (low-bandwidth, semantic, interactive)

All modes share the same WebSocket channel, which additionally carries
action log events from dag_executor — giving operators a synchronized
live view AND structured audit trail in one connection.
"""
