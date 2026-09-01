"""Cross-cutting platform services: config, ids, crypto, clock, telemetry.

Imports no layer above it (import-linter contract). Import as
``xiosync.platform`` — never as top-level ``platform`` (stdlib collision;
D-019).
"""
