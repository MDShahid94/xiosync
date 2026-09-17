"""xiosync.worker.dispatcher — backward-compat shim.

The webhook delivery dispatcher was renamed to ``webhook_dispatcher.py``.
All imports are re-exported from the new canonical location.

.. deprecated::
    Import from ``xiosync.worker.webhook_dispatcher`` instead.
"""
from __future__ import annotations
import warnings as _warnings

_warnings.warn(
    "xiosync.worker.dispatcher is deprecated; "
    "import from xiosync.worker.webhook_dispatcher instead.",
    DeprecationWarning,
    stacklevel=2,
)

# Re-export everything so existing code continues to work unchanged.
from xiosync.worker.webhook_dispatcher import (  # noqa: F401, E402
    dispatch_pending_webhooks,
    MAX_DELIVERY_ATTEMPTS,
    DELIVERY_TIMEOUT,
)
