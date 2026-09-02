"""ORM models — the metadata the migration chain is checked against.

Importing this package registers every model on ``Base.metadata`` so the
autogenerate-drift gate (INV-TEST-SCHEMA-2) sees the whole schema. Every new
model module MUST be imported here.
"""

from __future__ import annotations

from xiosync.persistence.models.artifacts import Artifact
from xiosync.persistence.models.authorization import Capability, Event, Grant
from xiosync.persistence.models.base import Base
from xiosync.persistence.models.identity import (
    Actor,
    AuthIdentity,
    Membership,
    Organization,
    Session,
)
from xiosync.persistence.models.ontology import (
    Edge,
    Memory,
    TypeRegistry,
    TypeRegistryAlias,
)
from xiosync.persistence.models.network import WorkerNetworkAllowRule
from xiosync.persistence.models.operations import Operation


from xiosync.persistence.models.plugins import (
    Plugin,
    PluginInstallation,
    PluginNetworkAllowRule,
    PluginRpcMethod,
)
from xiosync.persistence.models.registry import CapabilityGroup, RegistryCategory
from xiosync.persistence.models.documents import DocumentCollection, DocumentPage
from xiosync.persistence.models.secrets import SecretRef
from xiosync.persistence.models.sharing import ResourceShare
from xiosync.persistence.models.triggers import WorkflowTrigger
from xiosync.persistence.models.webhooks import WebhookSubscription
from xiosync.persistence.models.metering import UsageMeter
from xiosync.persistence.models.workers import WorkerCredential, WorkerEnrollment
from xiosync.persistence.models.workflows import (
    DeadLetter,
    Task,
    Workflow,
    WorkflowRun,
)
from xiosync.persistence.models.projects import Project
from xiosync.persistence.models.browser import (
    BrowserPool,
    BrowserSession,
    ComputeRuntime,
    RuntimeNode,
    MeshNetwork,
    MeshNode,
)

__all__ = [
    "Actor",
    "Artifact",
    "AuthIdentity",
    "Base",
    "Capability",
    "CapabilityGroup",
    "DeadLetter",
    "DocumentCollection",
    "DocumentPage",
    "Edge",
    "Event",
    "Grant",
    "Membership",
    "Memory",
    "Operation",
    "Organization",
    "Plugin",
    "Project",
    "PluginInstallation",
    "PluginNetworkAllowRule",
    "PluginRpcMethod",
    "RegistryCategory",
    "Session",
    "Task",
    "TypeRegistry",
    "TypeRegistryAlias",
    "Workflow",
    "WorkflowRun",
    "WorkerCredential",
    "WorkerEnrollment",
    "WorkerNetworkAllowRule",
    "WebhookSubscription",
    "SecretRef",
    "WorkflowTrigger",
    "ResourceShare",
    "UsageMeter",
    "BrowserPool",
    "BrowserSession",
    "ComputeRuntime",
    "RuntimeNode",
    "MeshNetwork",
    "MeshNode",
]
