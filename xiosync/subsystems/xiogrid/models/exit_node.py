"""SQLAlchemy ORM models: PPPoEHost, PPPoEExitNode, FingerprintProfile.

Multi-host design: each PPPoEHost represents one Mac Mini + Ubuntu VM pair.
PPPoEExitNode.ppp_slot is unique per host (0-980), not globally unique.
New hosts are registered at runtime — no code change required.
"""
from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import (
    Boolean, DateTime, Float, ForeignKey, Index,
    Integer, String, Text, UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from xiosync.persistence.models.base import Base
from xiosync.platform.ids import new_id


class PPPoEHost(Base):
    """One registered Mac Mini + Ubuntu VM pair.

    Each host contributes up to PPPoECeilings.MAX_SLOTS_PER_HOST (981)
    unique public IPs from its Airtel PPPoE line.
    New hosts can be added at any time via the /api/v1/pppoe/hosts endpoint.
    """
    __tablename__ = "xiogrid_pppoe_hosts"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=new_id
    )
    # Human-readable label
    name: Mapped[str] = mapped_column(String(120), nullable=False, unique=True)
    # e.g. "mac-mini-home", "mac-mini-office-2"
    description: Mapped[str] = mapped_column(Text, nullable=False, default="")

    # SSH access to the Ubuntu VM (not the Mac itself — Mac manages bridge only)
    vm_ssh_user: Mapped[str] = mapped_column(String(60), nullable=False, default="karmantu")
    vm_ssh_host: Mapped[str] = mapped_column(String(120), nullable=False)  # 192.168.54.132
    vm_ssh_port: Mapped[int] = mapped_column(Integer, nullable=False, default=22)
    vm_scripts_dir: Mapped[str] = mapped_column(String(200), nullable=False,
                                                  default="/usr/local/bin/xiogrid")
    # PPPoE parent interface on the VM (macvlan parent)
    pppoe_parent_iface: Mapped[str] = mapped_column(String(30), nullable=False, default="enp26s0")
    # PPPoE credentials (stored here; can migrate to secrets table later)
    pppoe_username: Mapped[str] = mapped_column(String(200), nullable=False)
    pppoe_password: Mapped[str] = mapped_column(String(200), nullable=False)
    # MAC OUI prefix for macvlan (e.g. "00:50:56:cc" = VMware OUI)
    mac_oui_prefix: Mapped[str] = mapped_column(String(20), nullable=False, default="00:50:56:cc")

    # Tailscale VM IP — Colab workers use this as exit-node target
    # e.g. "100.Y.Y.Y" after `tailscale up --advertise-exit-node` on VM
    tailscale_vm_ts_ip: Mapped[str | None] = mapped_column(String(45), nullable=True)

    # Capacity
    max_slots: Mapped[int] = mapped_column(Integer, nullable=False, default=981)
    warm_pool_target: Mapped[int] = mapped_column(Integer, nullable=False, default=50)

    # Lifecycle
    state: Mapped[str] = mapped_column(String(20), nullable=False, default="active")
    # "active" | "offline" | "maintenance"
    registered_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    last_seen: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    # Extensible metadata (e.g. ISP name, location, notes)
    meta: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default="{}"
    )


class FingerprintProfile(Base):
    """Device fingerprint profile — shared across all hosts.

    Profiles are global (not per-host). Each ppp slot is assigned
    a profile deterministically based on its slot index (see domain/pppoe.py).
    8 profiles cover Mac/Windows/Linux/Android diversity.
    """
    __tablename__ = "xiogrid_fingerprint_profiles"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=new_id
    )
    name: Mapped[str] = mapped_column(String(120), nullable=False, unique=True)
    os: Mapped[str] = mapped_column(String(20), nullable=False)
    # "macos" | "windows" | "linux" | "android"
    cores: Mapped[int] = mapped_column(Integer, nullable=False)
    ram_gb: Mapped[int] = mapped_column(Integer, nullable=False)
    webgl_vendor: Mapped[str] = mapped_column(String(200), nullable=False)
    webgl_renderer: Mapped[str] = mapped_column(String(200), nullable=False)
    platform: Mapped[str] = mapped_column(String(40), nullable=False)
    # navigator.platform value: "MacIntel" | "Win32" | "Linux x86_64" | "Linux armv81"
    ch_platform: Mapped[str] = mapped_column(String(40), nullable=False)
    ch_version: Mapped[str] = mapped_column(String(20), nullable=False)
    ch_arch: Mapped[str] = mapped_column(String(10), nullable=False)
    # "arm" | "x86"
    screen_width: Mapped[int] = mapped_column(Integer, nullable=False)
    screen_height: Mapped[int] = mapped_column(Integer, nullable=False)
    dpr: Mapped[float] = mapped_column(Float, nullable=False, default=1.0)
    cam_name: Mapped[str] = mapped_column(String(100), nullable=False)
    is_mobile: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    ua_template: Mapped[str] = mapped_column(Text, nullable=False)
    # {cv} → replaced with installed Chrome version at runtime
    canvas_seed: Mapped[int] = mapped_column(Integer, nullable=False)
    audio_seed: Mapped[int] = mapped_column(Integer, nullable=False)
    # ── Extended fingerprint fields (migration 0046) ───────────────────────────
    timezone: Mapped[str] = mapped_column(
        String(60), nullable=False, server_default="America/New_York"
    )
    # IANA tz string — must match PPPoE exit-node's geo-IP region for consistency.
    battery_level: Mapped[float] = mapped_column(
        Float, nullable=False, server_default="0.72"
    )
    # Navigator.getBattery() level (0.0–1.0). Default 0.72 = plausible mid-charge.
    connection_type: Mapped[str] = mapped_column(
        String(20), nullable=False, server_default="wifi"
    )
    # navigator.connection.effectiveType — "wifi" | "4g" | "ethernet"


class PPPoEExitNode(Base):
    """One PPPoE session slot on a specific VM (ppp0-ppp980).

    Uniqueness: (host_id, ppp_slot) — so two hosts can both have a slot 42.
    Global pool = union of all active hosts' slots.
    """
    __tablename__ = "xiogrid_pppoe_exit_nodes"
    __table_args__ = (
        UniqueConstraint("host_id", "ppp_slot", name="uq_host_ppp_slot"),
        Index("ix_pppoe_nodes_state", "state"),
        Index("ix_pppoe_nodes_host_state", "host_id", "state"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=new_id
    )
    host_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("xiogrid_pppoe_hosts.id", ondelete="CASCADE"),
        nullable=False,
    )
    fingerprint_profile_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("xiogrid_fingerprint_profiles.id", ondelete="RESTRICT"),
        nullable=False,
    )
    ppp_slot: Mapped[int] = mapped_column(Integer, nullable=False)  # 0-980

    # Live network state (updated by health check + provisioning)
    state: Mapped[str] = mapped_column(String(20), nullable=False, default="down")
    public_ip: Mapped[str | None] = mapped_column(String(45), nullable=True)
    # 223.181.x.x or 97.28.x.x (secondary Airtel pool)
    cgnat_ip: Mapped[str | None] = mapped_column(String(45), nullable=True)
    # 100.12.x.x (VM-side CGNAT address)

    # Current worker assignment
    assigned_worker_ts_ip: Mapped[str | None] = mapped_column(String(45), nullable=True)
    # Colab worker's Tailscale IP (used in ip rule on VM)
    assigned_session_id: Mapped[str | None] = mapped_column(String(200), nullable=True)
    assigned_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    # Metrics
    last_seen: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    last_health_check: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    last_speed_mbps: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    reconnect_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    total_sessions_served: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    # Per-slot SOCKS5 proxy (dante/microsocks)
    # Port formula: 10000 + ppp_slot (slots 0-980 → ports 10000-10980)
    # proxy_url is returned to Colab workers in acquire_any() response.
    # Each Chrome context uses its own proxy_url → different exit IP per context.
    # e.g. "socks5://100.106.81.15:10000" for slot 0
    proxy_port: Mapped[int | None] = mapped_column(Integer, nullable=True)
    proxy_url: Mapped[str | None] = mapped_column(String(100), nullable=True)
    # "running" | "stopped" | None
    proxy_state: Mapped[str | None] = mapped_column(String(20), nullable=True)

    meta: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default="{}"
    )
