"""XIOSYNC background worker package.

This package provides the background processes needed for production operation:
- Cron trigger ticker
- Lease expiry reaper
- Webhook delivery dispatcher
- Event trigger evaluator

Run with: ``python -m xiosync.worker.main``
"""
