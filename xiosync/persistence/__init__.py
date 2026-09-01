"""Repositories implementing domain-defined interfaces + the Alembic migration chain.

Every tenant-touching method requires OrgContext (RULE-ARCH-3; doc 05/06).
Migrations under ``xiosync/persistence/migrations/`` are the ONLY schema authority
(INV-SCHEMA-1).
"""
