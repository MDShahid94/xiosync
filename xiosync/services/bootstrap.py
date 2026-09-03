"""Genesis bootstrap — XIOSYNC self-registration (Phase 0b).

The ``BootstrapService`` creates the very first organization, actors, type
registry entries, and capability groups so XIOSYNC can govern itself using its
own protocols.  This is the resolution of Gap G-1 (no bootstrap API).

The bootstrap is **idempotent**: calling ``genesis()`` when the system org
already exists returns the existing state without modification.  This makes it
safe to call from both the CLI (first run) and the API (subsequent org
creation).

Design: every entity created during bootstrap is immediately recorded as an
``Operation`` and ``Event`` in the system's own audit trail, so the origin of
XIOSYNC itself is tracked.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from sqlalchemy import select, text
from sqlalchemy.orm import Session

from xiosync.persistence.models.authorization import Capability, Event, Grant
from xiosync.persistence.models.identity import (
    Actor,
    AuthIdentity,
    Membership,
    Organization,
)
from xiosync.persistence.models.ontology import TypeRegistry
from xiosync.persistence.models.operations import Operation
from xiosync.persistence.models.registry import CapabilityGroup, RegistryCategory
from xiosync.platform.crypto import hash_password
from xiosync.platform.ids import new_id

logger = logging.getLogger(__name__)

# -- Well-known sentinel UUIDs for the genesis org -----------------------------
# These are deterministic so repeated bootstrap calls are idempotent.

#: The XIOSYNC system organization — Org Zero.
GENESIS_ORG_ID = uuid.UUID("00000000-0000-7000-8000-000000000000")

#: System actor — the machine/platform itself.
SYSTEM_ACTOR_ID = uuid.UUID("00000000-0000-7000-8000-000000000001")

#: The first human actor — the developer bootstrapping the system.
BOOTSTRAP_HUMAN_ACTOR_ID = uuid.UUID("00000000-0000-7000-8000-000000000002")

#: AI agent actor — the AI assistant participating in development.
AI_AGENT_ACTOR_ID = uuid.UUID("00000000-0000-7000-8000-000000000003")


@dataclass(frozen=True, slots=True)
class GenesisResult:
    """Outcome of a genesis bootstrap operation."""

    organization_id: uuid.UUID
    system_actor_id: uuid.UUID
    human_actor_id: uuid.UUID
    ai_agent_actor_id: uuid.UUID
    auth_identity_id: uuid.UUID | None
    already_existed: bool


# -- Core type registry seed data ---------------------------------------------

_CORE_ACTOR_TYPES = [
    ("human", "Human user or developer"),
    ("ai_agent", "AI assistant or autonomous agent"),
    ("system", "Platform system actor (background workers, etc.)"),
    ("service", "External service or integration"),
    ("worker", "Compute worker node"),
]

_CORE_EVENT_TYPES = [
    # Existing runtime events
    ("action_executed", "An action was executed"),
    ("error", "An error occurred"),
    ("state_change", "A state transition occurred"),
    ("heartbeat", "A heartbeat signal"),
    ("tool_invoked", "A tool was invoked"),
    ("auth_event", "Authentication event"),
    ("metric", "A metric data point"),
    ("policy_decision", "An authorization policy decision"),
    ("task.output", "Streaming task output chunk"),
    ("webhook.dispatch", "Webhook delivery intent"),
    ("webhook.delivered", "Successful webhook delivery"),
    ("webhook.failed", "Failed webhook delivery"),
    # Genesis protocol — development events (Gap G-5)
    ("dev.commit", "A code commit was made"),
    ("dev.review", "A code review was submitted"),
    ("dev.merge", "A branch was merged"),
    ("ci.gate_passed", "A CI quality gate passed"),
    ("ci.gate_failed", "A CI quality gate failed"),
    ("deploy.started", "A deployment was started"),
    ("deploy.completed", "A deployment completed"),
    ("schema.migration_applied", "A database migration was applied"),
    ("schema.migration_rolled_back", "A database migration was rolled back"),
    ("protocol.evolution", "A protocol change was made to XIOSYNC itself"),
    # Genesis protocol — self-governance events
    ("genesis.bootstrap", "The system was bootstrapped"),
    ("actor.created", "An actor was created"),
    ("actor.registered", "An actor registered via API"),
    ("capability.created", "A capability was created"),
    ("capability.deprecated", "A capability was deprecated"),
    ("artifact.created", "An artifact was created"),
    ("share.created", "A resource share was created"),
    ("share.revoked", "A resource share was revoked"),
    ("workflow.created", "A workflow was created"),
    ("workflow.published", "A workflow was published"),
    ("trigger.created", "A trigger was created"),
    ("secret.created", "A secret reference was created"),
    ("secret.rotated", "A secret reference was rotated"),
    ("worker.registered", "A worker enrolled"),
    ("worker.approved", "A worker was approved"),
]

_CORE_LIFECYCLE_STATES = [
    ("proposed", "Entity proposed but not yet designed"),
    ("designing", "Entity is being designed"),
    ("implementing", "Entity is being implemented"),
    ("validating", "Entity is being validated"),
    ("initializing", "Entity is initializing"),
    ("active", "Entity is active and operational"),
    ("updating", "Entity is being updated"),
    ("suspended", "Entity is temporarily suspended"),
    ("migrating", "Entity is being migrated"),
    ("terminating", "Entity is being terminated"),
    ("terminated", "Entity has been terminated"),
    ("archived", "Entity has been archived"),
    # States used by browser/mesh/plugin tables — previously enforced by
    # CHECK constraints dropped in migration 0024 (Gap 1.2).  Adding them
    # here makes type_registry the single source of truth for lifecycle state
    # vocabulary.
    ("failed", "Entity encountered an unrecoverable failure"),
    ("ready", "Entity is provisioned and ready to accept work"),
    ("busy", "Entity is currently processing a request"),
    ("deprecated", "Entity is deprecated and pending removal"),
    ("configuring", "Entity is being configured"),
    ("error", "Entity is in an error state requiring attention"),
    ("draft", "Entity is in draft and not yet activated"),
    # Browser orchestration states
    ("offline", "Entity is offline / unreachable"),
    ("provisioning", "Entity is being provisioned"),
    ("disabled", "Entity has been administratively disabled"),
    # Plugin states
    ("registered", "Plugin is registered but not yet installed"),
    ("pending_approval", "Installation is pending approval"),
    ("approved", "Installation approved, awaiting activation"),
    ("revoked", "Entity access or installation has been revoked"),
]

_CORE_OPERATION_TYPES = [
    ("actor.state_change", "Actor lifecycle transition"),
    ("actor.create", "Actor creation"),
    ("genesis.bootstrap", "System genesis bootstrap"),
    ("capability.create", "Capability creation"),
    ("capability.deprecate", "Capability deprecation"),
    ("workflow.create", "Workflow creation"),
    ("workflow.publish", "Workflow publication"),
]

# -- Default capability groups (Q2-C: configurable RBAC) -----------------------

_DEFAULT_CAPABILITY_GROUPS: list[dict[str, Any]] = [
    {
        "name": "platform.admin",
        "description": "Full platform administration — all operations",
        "operations": ["*"],
    },
    {
        "name": "org.manage",
        "description": "Organization management operations",
        "operations": [
            "org.read", "org.update", "org.configure",
        ],
    },
    {
        "name": "actor.manage",
        "description": "Actor lifecycle management",
        "operations": [
            "actor.create", "actor.read", "actor.list",
            "actor.transition", "actor.suspend", "actor.terminate",
        ],
    },
    {
        "name": "workflow.manage",
        "description": "Workflow creation, publishing, and execution",
        "operations": [
            "workflow.create", "workflow.read", "workflow.list",
            "workflow.publish", "workflow.start_run", "workflow.enqueue_task",
        ],
    },
    {
        "name": "task.execute",
        "description": "Task leasing, heartbeat, completion, and checkpoint",
        "operations": [
            "task.lease", "task.heartbeat", "task.complete",
            "task.checkpoint", "task.claim_next",
        ],
    },
    {
        "name": "plugin.admin",
        "description": "Plugin installation, approval, and execution",
        "operations": [
            "plugin.install", "plugin.approve", "plugin.activate",
            "plugin.rpc", "plugin.read",
        ],
    },
    {
        "name": "event.manage",
        "description": "Event creation and reading",
        "operations": [
            "event.append", "event.read", "event.list", "event.stream",
        ],
    },
    {
        "name": "artifact.manage",
        "description": "Artifact creation and reading",
        "operations": [
            "artifact.create", "artifact.read", "artifact.list",
        ],
    },
    {
        "name": "secret.manage",
        "description": "Secret reference lifecycle management",
        "operations": [
            "secret.create", "secret.read", "secret.list",
            "secret.rotate", "secret.revoke",
        ],
    },
    {
        "name": "share.manage",
        "description": "Cross-org resource sharing management",
        "operations": [
            "share.create", "share.revoke", "share.list",
        ],
    },
    {
        "name": "worker.manage",
        "description": "Worker fleet lifecycle management",
        "operations": [
            "worker.register", "worker.approve", "worker.suspend",
            "worker.revoke", "worker.credential.issue",
        ],
    },
    {
        "name": "dlq.manage",
        "description": "Dead letter queue triage and resolution",
        "operations": [
            "dlq.read", "dlq.propose", "dlq.resolve",
        ],
    },
    {
        "name": "trigger.manage",
        "description": "Workflow trigger management",
        "operations": [
            "trigger.create", "trigger.read", "trigger.list",
            "trigger.pause", "trigger.resume",
        ],
    },
    {
        "name": "webhook.manage",
        "description": "Webhook subscription management",
        "operations": [
            "webhook.create", "webhook.read", "webhook.list",
            "webhook.pause",
        ],
    },
    {
        "name": "ontology.manage",
        "description": "Ontology graph edge and memory management",
        "operations": [
            "edge.create", "edge.read", "edge.list",
            "memory.create", "memory.update", "memory.read",
        ],
    },
    {
        "name": "capability.manage",
        "description": "Capability blueprint management",
        "operations": [
            "capability.create", "capability.read", "capability.list",
            "capability.deprecate",
        ],
    },
    {
        "name": "metering.read",
        "description": "Usage metering read access",
        "operations": [
            "metering.summary", "metering.history",
            "project.read", "project.list",
        ],
    },
    {
        "name": "project.read",
        "description": "Read-only access to project list and detail",
        "operations": [
            "project.list", "project.get",
        ],
    },
    {
        "name": "project.manage",
        "description": "Project management operations",
        "operations": [
            "project.create", "project.read", "project.list", "project.update", "project.archive",
        ],
    },
    {
        "name": "readonly",
        "description": "Read-only access to all list/get endpoints",
        "operations": [
            "workflow.read", "workflow.list",
            "task.read", "task.list",
            "event.read", "event.list",
            "artifact.read", "artifact.list",
            "capability.read", "capability.list",
            "worker.read", "worker.list",
            "dlq.read",
            "trigger.read", "trigger.list",
            "share.list",
            "metering.summary", "metering.history",
            "project.read", "project.list",
        ],
    },
]


class BootstrapService:
    """Genesis bootstrap — creates XIOSYNC as its own first organization.

    This service is the resolution of the Bootstrap Paradox: XIOSYNC needs an
    org to govern itself, but creating an org requires XIOSYNC.  The genesis
    method breaks this circle by creating the foundation entities directly,
    then recording the act through the system's own event/operation trail.
    """

    def __init__(self, session: Session) -> None:
        self._session = session

    def genesis(
        self,
        *,
        admin_email: str = "admin@xiosync.dev",
        admin_password: str | None = None,
        now: datetime | None = None,
    ) -> GenesisResult:
        """Bootstrap XIOSYNC — idempotent.

        Creates:
        1. The XIOSYNC system organization (Org Zero)
        2. Three actors: system, human developer, AI agent
        3. Core type_registry entries (actor types, event types, etc.)
        4. Default capability groups for RBAC
        5. Admin auth identity + membership (if password provided)
        6. Records the bootstrap as the first Operation + Event

        Returns ``GenesisResult`` with ``already_existed=True`` if the
        system org already exists.
        """
        from datetime import UTC

        if now is None:
            now = datetime.now(UTC)

        # Check if genesis already happened
        existing = self._session.execute(
            select(Organization).where(Organization.id == GENESIS_ORG_ID)
        ).scalar_one_or_none()

        if existing is not None:
            logger.info("genesis: system org already exists — skipping")
            return GenesisResult(
                organization_id=GENESIS_ORG_ID,
                system_actor_id=SYSTEM_ACTOR_ID,
                human_actor_id=BOOTSTRAP_HUMAN_ACTOR_ID,
                ai_agent_actor_id=AI_AGENT_ACTOR_ID,
                auth_identity_id=None,
                already_existed=True,
            )

        logger.info("genesis: creating XIOSYNC system organization")

        # 1. Create system organization
        org = Organization(
            id=GENESIS_ORG_ID,
            slug="xiosync-system",
            name="XIOSYNC System",
            state="active",
            resource_quotas={},
            created_at=now,
        )
        self._session.add(org)
        self._session.flush()

        # Set RLS context for this transaction
        self._session.execute(
            text("SELECT set_config('app.current_org', :org_id, true)"),
            {"org_id": str(GENESIS_ORG_ID)},
        )

        # 2. Create actors
        system_actor = Actor(
            id=SYSTEM_ACTOR_ID,
            organization_id=GENESIS_ORG_ID,
            actor_type="system",
            actor_subtype="platform",
            state="active",
            lifecycle_phase="operational",
            trust_tier="core",
            health_status="healthy",
            alias="XIOSYNC System",
            created_at=now,
        )
        human_actor = Actor(
            id=BOOTSTRAP_HUMAN_ACTOR_ID,
            organization_id=GENESIS_ORG_ID,
            actor_type="human",
            actor_subtype="developer",
            state="active",
            lifecycle_phase="operational",
            trust_tier="admin",
            health_status="healthy",
            alias="Bootstrap Admin",
            created_by=SYSTEM_ACTOR_ID,
            created_at=now,
        )
        ai_actor = Actor(
            id=AI_AGENT_ACTOR_ID,
            organization_id=GENESIS_ORG_ID,
            actor_type="ai_agent",
            actor_subtype="developer",
            state="active",
            lifecycle_phase="operational",
            trust_tier="core",
            health_status="healthy",
            alias="AI Development Agent",
            created_by=SYSTEM_ACTOR_ID,
            created_at=now,
        )
        self._session.add_all([system_actor, human_actor, ai_actor])
        self._session.flush()

        # 3. Seed type_registry with core entries
        self._seed_type_registry(now)

        # 4. Create default capability groups
        self._seed_capability_groups(now)

        # 5. Create admin identity if password provided
        auth_identity_id: uuid.UUID | None = None
        if admin_password:
            auth_identity_id = self._create_admin_identity(
                admin_email, admin_password, now
            )

        # 6. Record genesis as the first Operation + Event
        genesis_op_id = new_id()
        genesis_op = Operation(
            id=genesis_op_id,
            organization_id=GENESIS_ORG_ID,
            actor_id=SYSTEM_ACTOR_ID,
            operation="genesis.bootstrap",
            trigger="system",
            initiated_by=SYSTEM_ACTOR_ID,
            scope="organization",
            outcome="success",
            rationale="XIOSYNC genesis — self-bootstrapping as Org Zero",
            started_at=now,
            completed_at=now,
        )
        self._session.add(genesis_op)

        genesis_event = Event(
            id=new_id(),
            organization_id=GENESIS_ORG_ID,
            event_type="genesis.bootstrap",
            actor_id=SYSTEM_ACTOR_ID,
            severity="info",
            operation_id=genesis_op_id,
            entity_type="organization",
            entity_id=GENESIS_ORG_ID,
            payload={
                "summary": "XIOSYNC genesis — system bootstrapped as Org Zero",
                "organization_id": str(GENESIS_ORG_ID),
                "actors_created": [
                    str(SYSTEM_ACTOR_ID),
                    str(BOOTSTRAP_HUMAN_ACTOR_ID),
                    str(AI_AGENT_ACTOR_ID),
                ],
                "admin_email": admin_email if admin_password else None,
            },
            created_at=now,
        )
        self._session.add(genesis_event)

        self._session.flush()
        logger.info(
            "genesis: complete — org=%s, actors=%d, event=%s",
            GENESIS_ORG_ID,
            3,
            genesis_event.id,
        )

        return GenesisResult(
            organization_id=GENESIS_ORG_ID,
            system_actor_id=SYSTEM_ACTOR_ID,
            human_actor_id=BOOTSTRAP_HUMAN_ACTOR_ID,
            ai_agent_actor_id=AI_AGENT_ACTOR_ID,
            auth_identity_id=auth_identity_id,
            already_existed=False,
        )

    def _seed_type_registry(self, now: datetime) -> None:
        """Seed core type_registry entries for all categories."""
        entries: list[TypeRegistry] = []

        for value, desc in _CORE_ACTOR_TYPES:
            entries.append(
                TypeRegistry(
                    id=new_id(),
                    organization_id=None,
                    namespace="core",
                    category="actor_type",
                    value=value,
                    version=1,
                    state="active",
                    definition={"description": desc},
                    created_at=now,
                )
            )

        for value, desc in _CORE_EVENT_TYPES:
            entries.append(
                TypeRegistry(
                    id=new_id(),
                    organization_id=None,
                    namespace="core",
                    category="event_type",
                    value=value,
                    version=1,
                    state="active",
                    definition={"description": desc},
                    created_at=now,
                )
            )

        for value, desc in _CORE_LIFECYCLE_STATES:
            entries.append(
                TypeRegistry(
                    id=new_id(),
                    organization_id=None,
                    namespace="core",
                    category="lifecycle_state",
                    value=value,
                    version=1,
                    state="active",
                    definition={"description": desc},
                    created_at=now,
                )
            )

        for value, desc in _CORE_OPERATION_TYPES:
            entries.append(
                TypeRegistry(
                    id=new_id(),
                    organization_id=None,
                    namespace="core",
                    category="operation_type",
                    value=value,
                    version=1,
                    state="active",
                    definition={"description": desc},
                    created_at=now,
                )
            )

        self._session.add_all(entries)
        self._session.flush()
        logger.info("genesis: seeded %d type_registry entries", len(entries))

        # Populate the in-process event type cache so validate_event_type()
        # works immediately in this process without a DB round-trip.
        from xiosync.domain.event_registry import event_registry
        event_registry.register([value for value, _ in _CORE_EVENT_TYPES])

    def _seed_capability_groups(self, now: datetime) -> None:
        """Create default capability groups for RBAC."""
        groups: list[CapabilityGroup] = []
        for group_def in _DEFAULT_CAPABILITY_GROUPS:
            groups.append(
                CapabilityGroup(
                    id=new_id(),
                    organization_id=None,  # Global defaults
                    name=group_def["name"],
                    description=group_def["description"],
                    operations=group_def["operations"],
                    state="active",
                    created_at=now,
                )
            )
        self._session.add_all(groups)
        self._session.flush()
        logger.info("genesis: seeded %d capability groups", len(groups))

    def _create_admin_identity(
        self,
        email: str,
        password: str,
        now: datetime,
    ) -> uuid.UUID:
        """Create the bootstrap admin auth identity and membership."""
        identity_id = new_id()
        membership_id = new_id()

        identity = AuthIdentity(
            id=identity_id,
            organization_id=GENESIS_ORG_ID,
            human_actor_id=BOOTSTRAP_HUMAN_ACTOR_ID,
            email=email,
            password_hash=hash_password(password),
            state="active",
            failed_attempts=0,
            created_at=now,
        )
        membership = Membership(
            id=membership_id,
            organization_id=GENESIS_ORG_ID,
            auth_identity_id=identity_id,
            membership_role="org_owner",
            created_at=now,
        )

        self._session.add_all([identity, membership])
        self._session.flush()
        logger.info("genesis: admin identity created — email=%s", email)
        return identity_id
