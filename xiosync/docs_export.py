"""CLI module to generate XIOSYNC's README.md from the codebase analysis.

This module acts as the export mechanism for the system's self-documentation,
writing the normative README.md to the project root.
"""

from __future__ import annotations

import logging
import sys
from pathlib import Path

logger = logging.getLogger(__name__)

README_CONTENT = """# XIOSYNC

**XIOSYNC** is an autonomous workflow orchestration platform with multi-tenant governance. It allows human, AI, and system actors to collaborate safely in bounded tenant environments.

## Project Overview

XIOSYNC is built to manage workflows and coordinate agents with strict governance, auditability, and access control. It implements an evolutionary protocol where changes to the system itself are tracked as governed events.

## Architecture

XIOSYNC uses a strict 4-layer architecture enforced by `import-linter`. Dependencies flow downward only:

```mermaid
graph TD
    API[API Layer / FastAPI] --> Services[Services Layer]
    Services --> Persistence[Persistence Layer / SQLAlchemy]
    Persistence --> Domain[Domain Layer / Pure Python]
    
    Platform[Platform / Cross-cutting]
```

- **Domain**: Pure Python domain models, errors, and interfaces. No framework or persistence logic.
- **Persistence**: SQLAlchemy ORM models, repository patterns, database access.
- **Services**: Business logic, transactional boundaries.
- **API**: FastAPI composition root, routing, middleware (RBAC, Rate Limiting, Version Governance).
- **Platform**: Cross-cutting utilities (clock, config, IDs, telemetry).

## Self-Governance

XIOSYNC is an evolutionary entity that governs itself. 
- **Org Zero**: Bootstrapped via `BootstrapService.genesis()`, XIOSYNC operates as the system organization (Org Zero) to manage its own entities.
- **Protocol Evolution**: The `ProtocolService` records schema migrations, protocol upgrades, and capability changes as first-class `Operation` and `Event` records within the system.

## Data Model

Key entities include:
- `Organization`: The core tenant boundary. All tenant data is tied to an organization.
- `Actor`: Any participating entity (human, AI agent, system worker, service).
- `MemberAuth`: Login credentials for a human actor.
- `Membership`: Maps an identity to a role within an organization.
- `Session`: Revocable authentication sessions.
- `Event` & `Operation`: Full audit trail of actions taken in the system.
- `DocumentCollection` & `DocumentPage`: Governed document storage.

```mermaid
erDiagram
    Organization ||--o{ Actor : has
    Organization ||--o{ MemberAuth : has
    MemberAuth ||--o{ Membership : holds
    Actor ||--o{ Event : performs
```


## XIOGRID Capabilities (Decoupling)

XIOSYNC supports comprehensive capabilities inherited from the XIOGRID decoupling:

- **Browser Orchestration**: Management of `BrowserPool` and `BrowserSession` resources to control isolated browser environments dynamically.
- **Compute Runtimes**: Provisioning and monitoring of `ComputeRuntime` and `RuntimeNode` entities for scalable execution.
- **Mesh Networks**: Orchestration of `MeshNetwork` and `MeshNode` for seamless and secure peer-to-peer communication across runtime nodes.

These capabilities are fully integrated into the RBAC model, audit trail (Operations), and event streaming systems.

## API Surface

The API is exposed via FastAPI routers grouped by capability:

- `auth`: Authentication and session management (Public)
- `organizations`: Org management and bootstrap
- `actors`: Actor lifecycle (`actor.manage`)
- `workflows_crud`, `batch`: Workflow management (`workflow.manage`)
- `execution`, `task_streams`: Task execution (`task.execute`)
- `dlq`: Dead letter queue (`dlq.manage`)
- `plugins`: Plugin administration (`plugin.admin`)
- `listings`: Read-only queries (`readonly`)
- `streaming`, `operations`: Event streams and operations (`event.manage`)
- `triggers`: Workflow triggers (`trigger.manage`)
- `metering`: Resource usage (`metering.read`)
- `workers_crud`: Worker management (`worker.manage`)
- `secrets_crud`: Secrets management (`secret.manage`)
- `shares`: Cross-org sharing (`share.manage`)
- `webhooks`: Webhooks (`webhook.manage`)
- `ontology`: Ontology / Type Registry (`ontology.manage`)
- `protocol`, `capability_groups`: Protocol and capabilities (`capability.manage`)

## RBAC Model

XIOSYNC uses a role and capability-based access control system:
1. **Roles**: Identities hold a `MembershipRole` (`org_owner`, `org_admin`, `org_member`, `org_viewer`).
2. **Capability Groups**: Roles map to Capability Groups (e.g., `platform.admin`, `workflow.manage`, `readonly`).
3. **Enforcement**: API routes are protected by `require_capability("<cap>")` dependencies which evaluate the actor's effective permissions based on their organization context.

## Document Management

XIOSYNC includes a built-in enterprise document management system using dedicated tables:
- `DocumentCollection`: Versioned, stateful collections of documents.
- `DocumentPage`: Hierarchical, ordered pages within a collection.

## AI-Agent Documentation

To support autonomous agents, XIOSYNC generates LLM-friendly documentation formats via the `DocumentService`:
- `llms.txt`: A markdown index of a document collection's pages.
- `llms-full.txt`: A concatenated full text of all pages in a collection.

## Getting Started & Development

XIOSYNC requires Python 3.13 and uses `uv` (or `hatch`) for dependency management.

**Run tests:**
```bash
DATABASE_URL="postgresql+psycopg://xiosync_test:xiosync_test@localhost:5432/xiosync_ci" uv run pytest tests/unit/
```

**Type checking:**
```bash
uv run mypy xiosync/
```

**Linting (including architecture bounds):**
```bash
uv run ruff check xiosync/
uv run import-linter
```

**Database Migrations:**
```bash
uv run alembic upgrade head
```
"""


def export_readme(target_path: Path | None = None) -> None:
    """Generate and write the README.md file."""
    if target_path is None:
        # Default to project root (assuming this script is in xiosync/)
        target_path = Path(__file__).parent.parent / "README.md"
        
    target_path.write_text(README_CONTENT, encoding="utf-8")
    print(f"Successfully exported README to {target_path.resolve()}")


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    try:
        # Allow passing an explicit path as a CLI argument
        path = Path(sys.argv[1]) if len(sys.argv) > 1 else None
        export_readme(path)
    except Exception as e:
        logger.error(f"Failed to export README: {e}")
        sys.exit(1)
