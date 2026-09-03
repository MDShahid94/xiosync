"""Backward-compat shim — WorkflowTemplateService moved to xiosync.subsystems.xiogrid."""
from xiosync.subsystems.xiogrid.services.workflow_templates import *  # noqa: F401, F403
from xiosync.subsystems.xiogrid.services.workflow_templates import (  # noqa: F401
    WorkflowTemplateService,
    TemplateRecord,
)
