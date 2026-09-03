"""XIOGRID subsystem bootstrap — Type Registry extensions and capability groups."""

from __future__ import annotations

import logging
from datetime import datetime

from sqlalchemy.orm import Session

from xiosync.domain.context import OrgContext
from xiosync.persistence.models.ontology import TypeRegistry
from xiosync.persistence.models.registry import CapabilityGroup, RegistryCategory
from xiosync.platform.ids import new_id
from xiosync.services.projects import ProjectService

logger = logging.getLogger(__name__)

# -- Type definitions --

_BROWSER_ENGINE_TYPES = [
    ("chrome", "Chromium-based browser"),
    ("firefox", "Firefox browser"),
    ("webkit", "WebKit-based browser"),
    ("custom", "Custom browser engine"),
]

_BROWSER_POOL_STATES = [
    ("active", "Pool is active and running"),
    ("scaling", "Pool is scaling up or down"),
    ("draining", "Pool is draining sessions"),
    ("terminated", "Pool is terminated"),
]

_BROWSER_SESSION_STATES = [
    ("initializing", "Session is initializing"),
    ("active", "Session is active"),
    ("suspended", "Session is suspended"),
    ("terminated", "Session has terminated"),
    ("failed", "Session failed"),
]

_COMPUTE_PROVIDER_TYPES = [
    ("colab", "Google Colab"),
    ("aws_ec2", "AWS EC2"),
    ("gcp_compute", "Google Cloud Compute Engine"),
    ("azure_vm", "Azure Virtual Machines"),
    ("bare_metal", "Bare Metal Server"),
    ("docker", "Docker Container"),
    ("custom", "Custom Compute Provider"),
]

_RUNTIME_NODE_STATES = [
    ("provisioning", "Node is provisioning"),
    ("booting", "Node is booting"),
    ("ready", "Node is ready"),
    ("running", "Node is running workloads"),
    ("draining", "Node is draining workloads"),
    ("terminated", "Node is terminated"),
    ("failed", "Node failed"),
]

_MESH_NETWORK_PROVIDERS = [
    ("tailscale", "Tailscale Mesh Network"),
    ("wireguard", "WireGuard Mesh Network"),
    ("nebula", "Nebula Mesh Network"),
    ("custom", "Custom Mesh Network"),
]

_MESH_NETWORK_STATES = [
    ("active", "Mesh network is active"),
    ("degraded", "Mesh network is degraded"),
    ("offline", "Mesh network is offline"),
]

_WORKFLOW_TEMPLATE_CATEGORIES = [
    ("browser_automation", "Browser automation workflow"),
    ("auth_flow", "Authentication flow workflow"),
    ("infrastructure", "Infrastructure provisioning workflow"),
    ("maintenance", "System maintenance workflow"),
]

_EVENT_TYPES = [
    ("browser_pool.created", "Browser pool was created"),
    ("browser_pool.scaled", "Browser pool was scaled"),
    ("browser_pool.destroyed", "Browser pool was destroyed"),
    ("browser_session.created", "Browser session created"),
    ("browser_session.verified", "Browser session verified"),
    ("browser_session.terminated", "Browser session terminated"),
    ("compute_runtime.created", "Compute runtime was created"),
    ("mesh_network.created", "Mesh network was created"),
    ("mesh_network.node_added", "Mesh network node added"),
    ("mesh_network.node_removed", "Mesh network node removed"),
]

_NEW_CAPABILITY_GROUPS = [
    {
        "name": "browser_pool.manage",
        "description": "Browser pool management",
        "operations": [
            "browser_pool.*"
        ],
    },
    {
        "name": "browser_session.manage",
        "description": "Browser session management",
        "operations": [
            "browser_session.*"
        ],
    },
    {
        "name": "compute_runtime.manage",
        "description": "Compute runtime management",
        "operations": [
            "compute_runtime.*", "runtime_node.*"
        ],
    },
    {
        "name": "mesh_network.manage",
        "description": "Mesh network management",
        "operations": [
            "mesh.network.*"
        ],
    },
]


def register_xiobr_types(session: Session, context: OrgContext, now: datetime | None = None) -> None:
    """Register XIOBR-decoupled types into the TypeRegistry and create capability groups."""
    from datetime import UTC
    
    if now is None:
        now = datetime.now(UTC)

    entries: list[TypeRegistry] = []

    def _add_entries(category: str, pairs: list[tuple[str, str]]) -> None:
        for value, desc in pairs:
            entries.append(
                TypeRegistry(
                    id=new_id(),
                    organization_id=context.organization_id,
                    namespace="xiobr",
                    category=category,
                    value=value,
                    version=1,
                    state="active",
                    definition={"description": desc},
                    created_at=now,
                )
            )

    _add_entries("browser_engine_type", _BROWSER_ENGINE_TYPES)
    _add_entries("browser_pool_state", _BROWSER_POOL_STATES)
    _add_entries("browser_session_state", _BROWSER_SESSION_STATES)
    _add_entries("compute_provider_type", _COMPUTE_PROVIDER_TYPES)
    _add_entries("runtime_node_state", _RUNTIME_NODE_STATES)
    _add_entries("mesh_network_provider", _MESH_NETWORK_PROVIDERS)
    _add_entries("mesh_network_state", _MESH_NETWORK_STATES)
    _add_entries("workflow_template_category", _WORKFLOW_TEMPLATE_CATEGORIES)
    _add_entries("event_type", _EVENT_TYPES)

    session.add_all(entries)
    logger.info("xiobr_bootstrap: registered %d type_registry entries", len(entries))

    groups: list[CapabilityGroup] = []
    for group_def in _NEW_CAPABILITY_GROUPS:
        groups.append(
            CapabilityGroup(
                id=new_id(),
                organization_id=context.organization_id,
                name=group_def["name"],
                description=str(group_def["description"]),
                operations=list(group_def["operations"]),
                state="active",
                created_at=now,
            )
        )

    session.add_all(groups)
    logger.info("xiobr_bootstrap: created %d capability groups", len(groups))
    session.flush()

    # Populate the in-process event type cache with XIOBR-specific event types.
    from xiosync.domain.event_registry import event_registry
    event_registry.register([value for value, _ in _EVENT_TYPES])

    project_svc = ProjectService(session)
    project_svc.create_project(
        context,
        name="XIO Browser",
        slug="xio-browser",
        description="Home for all XIOBR resources",
    )
    logger.info("xiobr_bootstrap: created xio-browser project")


# Canonical public name for the subsystem entry point.
register_xiogrid = register_xiobr_types
