import uuid
from datetime import datetime

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from xiosync.domain.organizations import BrandingRecord, BrandingUpdate
from xiosync.persistence.models.organizations import OrganizationBranding


class OrganizationService:
    def __init__(self, session: Session):
        self.session = session

    def get_branding(self, organization_id: uuid.UUID) -> BrandingRecord | None:
        stmt = select(OrganizationBranding).where(
            OrganizationBranding.organization_id == organization_id
        )
        model = self.session.execute(stmt).scalar_one_or_none()

        if not model:
            return None

        return BrandingRecord(
            organization_id=model.organization_id,
            theme_mode=model.theme_mode,
            primary_color=model.primary_color,
            logo_url=model.logo_url,
            favicon_url=model.favicon_url,
            custom_domain=model.custom_domain,
            created_at=model.created_at,
            updated_at=model.updated_at,
        )

    def update_branding(
        self, organization_id: uuid.UUID, update_data: BrandingUpdate
    ) -> BrandingRecord:
        # Check if exists
        existing = self.get_branding(organization_id)

        if existing:
            # Update
            values = {}
            if update_data.theme_mode is not None:
                values["theme_mode"] = update_data.theme_mode
            if update_data.primary_color is not None:
                values["primary_color"] = update_data.primary_color
            if update_data.logo_url is not None:
                values["logo_url"] = update_data.logo_url
            if update_data.favicon_url is not None:
                values["favicon_url"] = update_data.favicon_url
            if update_data.custom_domain is not None:
                values["custom_domain"] = update_data.custom_domain

            if values:
                values["updated_at"] = datetime.utcnow()
                stmt = (
                    update(OrganizationBranding)
                    .where(OrganizationBranding.organization_id == organization_id)
                    .values(**values)
                )
                self.session.execute(stmt)
        else:
            # Create
            new_branding = OrganizationBranding(
                organization_id=organization_id,
                theme_mode=update_data.theme_mode
                if update_data.theme_mode is not None
                else "system",
                primary_color=update_data.primary_color,
                logo_url=update_data.logo_url,
                favicon_url=update_data.favicon_url,
                custom_domain=update_data.custom_domain,
            )
            self.session.add(new_branding)

        self.session.flush()
        return self.get_branding(organization_id)
