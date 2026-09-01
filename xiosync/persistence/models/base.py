"""Declarative base for all ORM models (doc 06 §4-5).

One metadata object, one deterministic naming convention. Deterministic
constraint names are load-bearing: the autogenerate-diff-must-be-empty CI gate
(INV-TEST-SCHEMA-2) compares this metadata against the migrated database, and
non-deterministic names would produce phantom drift.

Schema authority note (INV-SCHEMA-1): this metadata is *compared* against the
database, never applied to it. ``metadata.create_all()`` is forbidden
everywhere in application and test code; only ``xiosync/persistence/migrations/``
creates schema.
"""

from __future__ import annotations

from sqlalchemy import MetaData
from sqlalchemy.orm import DeclarativeBase

NAMING_CONVENTION: dict[str, str] = {
    "ix": "ix_%(column_0_label)s",
    "uq": "uq_%(table_name)s_%(column_0_N_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_N_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


class Base(DeclarativeBase):
    """Root declarative base; every table's model inherits from this."""

    metadata = MetaData(naming_convention=NAMING_CONVENTION)
