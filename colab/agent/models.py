"""colab.agent.models — Pydantic request/response models and StrEnums."""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

from pydantic import BaseModel


class LaunchRequest(BaseModel):
    session_id: str
    proxy_url: str | None = None
    profile_dir: str | None = None  # already-extracted local dir (or None = fresh)
    fingerprint: dict[str, Any] = {}
    headless: bool = False  # False = Xvfb headed (vastly more undetectable)
    exit_node_public_ip: str | None = None  # PPPoE slot public IP for timezone geo-resolve


class TerminateRequest(BaseModel):
    session_id: str


class PullProfileRequest(BaseModel):
    identity_id: str
    # Accept both names: drive_object_key (new canonical) and r2_object_key (deprecated)
    drive_object_key: str | None = None
    r2_object_key: str | None = None  # deprecated — use drive_object_key
    # If provided, extract profile to this exact dir (e.g. /tmp/uc-profile-SESSION_ID)
    # instead of PROFILES_BASE. Used by signin flow for profile reuse.
    target_dir: str | None = None

    @property
    def object_key(self) -> str:
        """Resolve whichever key name the caller used."""
        key = self.drive_object_key or self.r2_object_key or ""
        if not key:
            raise ValueError("Either drive_object_key or r2_object_key must be provided")
        return key


class PushProfileRequest(BaseModel):
    identity_id: str
    local_dir: str
    # Accept both names: drive_object_key (new canonical) and r2_object_key (deprecated)
    drive_object_key: str | None = None
    r2_object_key: str | None = None  # deprecated — use drive_object_key

    @property
    def object_key(self) -> str:
        """Resolve whichever key name the caller used."""
        key = self.drive_object_key or self.r2_object_key or ""
        if not key:
            raise ValueError("Either drive_object_key or r2_object_key must be provided")
        return key


class UCLoginRequest(BaseModel):
    session_id: str | None = None  # look up proxy_url + UA from _sessions
    email: str
    password: str
    totp_secret: str
    proxy_url: str | None = None  # explicit override; falls back to session's proxy
    exit_node_public_ip: str | None = None  # PPPoE slot public IP for timezone geo-resolve
    identity_id: str | None = None  # if set, login-start uses pre-pulled PRFL profile
    fingerprint: dict = {}  # Pass full FingerprintSpec


class CDPExposeRequest(BaseModel):
    session_id: str
    local_port: int  # Chrome CDP port on 127.0.0.1
    public_port: int = 0  # 0 = auto-pick a free port


class CDPProxyRequest(BaseModel):
    session_id: str | None = None


class UCNavigateRequest(BaseModel):
    session_id: str | None = None
    url: str
    wait_seconds: float = 10.0


class UCScreenshotRequest(BaseModel):
    session_id: str | None = None


class InjectCookiesRequest(BaseModel):
    session_id: str
    cookies: list[dict]  # raw cookie dicts from /run-uc-login
    verify_url: str = "https://myaccount.google.com/"


class NavigateRequest(BaseModel):
    session_id: str
    url: str
    wait_until: str = "domcontentloaded"  # domcontentloaded | load | networkidle


class HITLState(StrEnum):
    PENDING = "PENDING"
    RESUMED = "RESUMED"
    EXPIRED = "EXPIRED"
    CANCELLED = "CANCELLED"


class HITLResumedBy(StrEnum):
    HUMAN = "HUMAN"
    AI_AGENT = "AI_AGENT"
    TIMEOUT = "TIMEOUT"


@dataclass
class HITLNotice:
    organization_id: uuid.UUID
    session_id: str
    challenge_type: str
    message: str
    id: uuid.UUID = field(default_factory=uuid.uuid4)
    identity_id: uuid.UUID | None = None
    instructions: str | None = None
    state: HITLState = HITLState.PENDING
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    resumed_at: datetime | None = None
    resumed_by: HITLResumedBy | None = None


class HITLCreateRequest(BaseModel):
    session_id: str
    challenge_type: str
    message: str
    instructions: str | None = None
    screenshot_path: str | None = None


class HITLResumeRequest(BaseModel):
    resumed_by: str = "human"  # human, ai_agent


class EvictDomainRequest(BaseModel):
    profile_dir: str | None = None
    domain_pattern: str
    cascade: bool = True


class CascadeCheckRequest(BaseModel):
    identity_id: str
    email: str
    proxy_url: str | None = None


class PersistSessionRequest(BaseModel):
    identity_id: str
    org_id: str = "00000000-0000-7000-8000-000000000000"
    email: str | None = None  # used for fingerprint.json
    cookies: list[dict] = []
    profile_dir: str | None = None
    page_url: str | None = None


class AIGenerateRequest(BaseModel):
    prompt: str
    system: str = ""
    output_format: str = "text"
    json_schema: str | None = None
    temperature: float = 0.2
    max_tokens: int = 8192
    timeout: int = 120
    model: str | None = None
    provider: str | None = None  # "local", "remote", or None (auto)
