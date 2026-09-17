from __future__ import annotations

import copy
import logging
import re
from typing import Any

logger = logging.getLogger(__name__)

class PIIScrubber:
    """Deterministic PII redaction engine."""

    SENSITIVE_KEYS = {
        'password', 'secret', 'token', 'api_key', 'apikey', 'auth',
        'credential', 'ssn', 'credit_card'
    }

    PATTERNS = {
        'EMAIL': r'\b[\w\.-]+@[\w\.-]+\.\w{2,}\b',
        'PHONE': r'(?:\+\d{1,3}[\s.-]?)?\(?\d{3}\)?[\s.-]?\d{3}[\s.-]?\d{4}\b',
        'CREDIT_CARD': r'\b(?:\d{4}[\s-]?){3}\d{1,4}\b',
        'SSN': r'\b\d{3}-\d{2}-\d{4}\b',
        'IP_ADDRESS': r'\b(?:(?:25[0-5]|2[0-4]\d|[01]?\d\d?)\.){3}(?:25[0-5]|2[0-4]\d|[01]?\d\d?)\b',
        'JWT_TOKEN': r'\beyJ[A-Za-z0-9_-]{10,}\.eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\b'
    }

    @staticmethod
    def scrub_string(value: str) -> str:
        """Applies all regex patterns, replacing with <TYPE_REDACTED>."""
        scrubbed = value
        for ptype, pattern in PIIScrubber.PATTERNS.items():
            scrubbed = re.sub(pattern, f'<{ptype}_REDACTED>', scrubbed)
        return scrubbed

    @staticmethod
    def scrub_dict(data: dict[str, Any]) -> dict[str, Any]:
        """Deep-walks dict, redacts sensitive keys entirely, scrubs string values."""
        result = {}
        for key, value in data.items():
            if any(s_key in key.lower() for s_key in PIIScrubber.SENSITIVE_KEYS):
                result[key] = '<KEY_REDACTED>'
                continue

            if isinstance(value, dict):
                result[key] = PIIScrubber.scrub_dict(value)
            elif isinstance(value, list):
                result[key] = [
                    PIIScrubber.scrub_dict(item) if isinstance(item, dict)
                    else PIIScrubber.scrub_string(item) if isinstance(item, str)
                    else item
                    for item in value
                ]
            elif isinstance(value, str):
                result[key] = PIIScrubber.scrub_string(value)
            else:
                result[key] = value
        return result

    @staticmethod
    def redact_place_value(place_value: dict[str, Any]) -> dict[str, Any]:
        """Specialized scrubber for place_value that strips value=, placeholder= from locators."""
        pv = copy.deepcopy(place_value)

        def scrub_locators(locs: list[str]) -> list[str]:
            scrubbed = []
            for loc in locs:
                loc = re.sub(r'''(value|placeholder)=['"][^'"]*['"]''', r'\1="<REDACTED>"', loc, flags=re.IGNORECASE)
                scrubbed.append(loc)
            return scrubbed

        if 'locators' in pv and isinstance(pv['locators'], list):
            pv['locators'] = scrub_locators(pv['locators'])

        return PIIScrubber.scrub_dict(pv)

    @staticmethod
    def is_public_domain(url: str) -> bool:
        """Returns False for localhost, 192.168.x.x, 10.x.x.x, 127.0.0.1, .internal, .corp, .local domains."""
        private_patterns = [
            r'localhost',
            r'^127\.0\.0\.1$',
            r'^10\.',
            r'^192\.168\.',
            r'\.internal$',
            r'\.corp$',
            r'\.local$'
        ]

        domain = re.sub(r'^https?://', '', url).split('/')[0].split(':')[0]

        for pattern in private_patterns:
            if re.search(pattern, domain, flags=re.IGNORECASE):
                return False
        return True
