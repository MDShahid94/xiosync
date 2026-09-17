import dataclasses
import uuid
from datetime import datetime


@dataclasses.dataclass(frozen=True)
class BrandingRecord:
    organization_id: uuid.UUID
    theme_mode: str
    primary_color: str | None
    logo_url: str | None
    favicon_url: str | None
    custom_domain: str | None
    created_at: datetime
    updated_at: datetime | None


@dataclasses.dataclass(frozen=True)
class BrandingUpdate:
    theme_mode: str | None = None
    primary_color: str | None = None
    logo_url: str | None = None
    favicon_url: str | None = None
    custom_domain: str | None = None
