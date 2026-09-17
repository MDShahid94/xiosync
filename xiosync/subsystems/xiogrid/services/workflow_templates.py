"""Backward-compatibility shim — WorkflowTemplateService moved to xioflow.

The canonical implementation lives at:
    xiosync.subsystems.xioflow.services.templates

All existing callers continue to work through this re-export.
"""
from xiosync.subsystems.xioflow.services.templates import (  # noqa: F401
    WorkflowTemplateNotFoundError,
    WorkflowTemplateRecord,
    WorkflowTemplateService,
)

__all__ = [
    "WorkflowTemplateNotFoundError",
    "WorkflowTemplateRecord",
    "WorkflowTemplateService",
]
