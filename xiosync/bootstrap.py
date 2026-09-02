"""CLI genesis bootstrap — ``python -m xiosync.bootstrap``.

This is the CLI entry point for bootstrapping XIOSYNC as its own first
organization.  It breaks the Bootstrap Paradox: the API requires auth,
auth requires an identity, identities require an org, and orgs can only be
created through the API.  This CLI bypasses the API to create the genesis
state directly in the database.

Usage::

    # Minimal — creates org + actors + type registry + capability groups
    python -m xiosync.bootstrap

    # With admin identity (email + password for API login)
    python -m xiosync.bootstrap --admin-email admin@xiosync.dev --admin-password Password1234

    # Custom database URL
    DATABASE_URL="postgresql+psycopg://..." python -m xiosync.bootstrap
"""

from __future__ import annotations

import argparse
import logging
import os
import sys


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="xiosync.bootstrap",
        description="Bootstrap XIOSYNC as its own first organization (genesis)",
    )
    parser.add_argument(
        "--admin-email",
        default="admin@xiosync.dev",
        help="Email for the bootstrap admin identity (default: admin@xiosync.dev)",
    )
    parser.add_argument(
        "--admin-password",
        default=None,
        help="Password for the bootstrap admin identity (omit to skip identity creation)",
    )
    parser.add_argument(
        "--database-url",
        default=None,
        help="PostgreSQL URL (default: DATABASE_URL env var)",
    )
    parser.add_argument(
        "--verbose", "-v",
        action="store_true",
        help="Enable verbose logging",
    )
    args = parser.parse_args()

    # Configure logging
    log_level = logging.DEBUG if args.verbose else logging.INFO
    logging.basicConfig(
        level=log_level,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        stream=sys.stderr,
    )
    logger = logging.getLogger("xiosync.bootstrap")

    # Resolve database URL
    database_url = args.database_url or os.environ.get("DATABASE_URL")
    if not database_url:
        logger.critical("DATABASE_URL environment variable is required")
        sys.exit(1)

    # Ensure it's PostgreSQL
    if not database_url.startswith(("postgresql://", "postgresql+psycopg://")):
        logger.critical("DATABASE_URL must start with postgresql:// or postgresql+psycopg://")
        sys.exit(1)

    try:
        from sqlalchemy import create_engine
        from sqlalchemy.orm import Session as SASession

        from xiosync.services.bootstrap import BootstrapService

        engine = create_engine(database_url, pool_pre_ping=True)

        with SASession(engine) as session:
            with session.begin():
                service = BootstrapService(session)
                result = service.genesis(
                    admin_email=args.admin_email,
                    admin_password=args.admin_password,
                )

        if result.already_existed:
            logger.info("Genesis already completed — system org exists")
            print(f"✓ System org already exists: {result.organization_id}")
        else:
            logger.info("Genesis bootstrap complete!")
            print(f"✓ Organization: {result.organization_id}")
            print(f"  System actor: {result.system_actor_id}")
            print(f"  Human actor:  {result.human_actor_id}")
            print(f"  AI actor:     {result.ai_agent_actor_id}")
            if result.auth_identity_id:
                print(f"  Auth identity: {result.auth_identity_id}")
                print(f"  Admin email:   {args.admin_email}")
            else:
                print("  (No admin identity created — use --admin-password to create one)")
            print()
            print("XIOSYNC is now self-governing. All entities are tracked in the audit trail.")

    except Exception as exc:
        logger.critical("Genesis bootstrap failed: %s", exc, exc_info=True)
        sys.exit(1)


if __name__ == "__main__":
    main()
