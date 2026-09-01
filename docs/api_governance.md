# XIOSYNC API Versioning and Deprecation Governance Specification

## 1. Purpose

This document defines the official API versioning strategy, deprecation
lifecycle, and backward-compatibility guarantees for the XIOSYNC platform.

## 2. Version Lifecycle

Every API version follows a four-stage lifecycle:

| Stage        | Description                                           |
|-------------|-------------------------------------------------------|
| **Current**  | Default version. Fully supported. All new features.   |
| **Deprecated** | Functional but scheduled for removal. `Deprecation` header set. |
| **Sunset**   | No longer accepting new requests. `Sunset` header present. Returns 410 Gone. |
| **Removed**  | Endpoint removed from codebase.                       |

## 3. Version Naming

- Versions follow the `/api/v{N}` URL prefix convention.
- Only **major** versions are exposed in the URL path (e.g., `/api/v1`, `/api/v2`).
- Minor/patch changes are backward-compatible and don't change the URL.

## 4. Response Headers

Every response includes:

```
X-API-Version: 1.0
```

When an endpoint is deprecated:

```
Deprecation: 2027-01-01
Sunset: 2027-06-01
```

These comply with:
- **RFC 8594** (The Sunset HTTP Header Field)
- **draft-ietf-httpapi-deprecation-header** (The Deprecation HTTP Header Field)

## 5. Request Negotiation

Clients may include an `Accept-Version` request header to indicate their
preferred API version. The platform echoes the acknowledged version in
`X-Accepted-Version`. This is reserved for future multi-version support.

## 6. Deprecation Window

- **Minimum deprecation window**: 6 months from `Deprecation` to `Sunset`.
- **Minimum sunset window**: 3 months from `Sunset` to removal.
- Announcements are communicated via:
  - HTTP response headers (automated)
  - Platform changelog
  - Organization admin notifications (if configured)

## 7. Backward Compatibility Guarantees

Within a major version, the following are guaranteed:
- Existing fields in response bodies will not be removed.
- Existing request body fields will not become required.
- HTTP status codes for success cases will not change.
- URL paths will not change.

The following may change without a version bump:
- Adding new optional fields to request/response bodies.
- Adding new endpoints.
- Adding new query parameters (always optional).
- Adding new HTTP headers.

## 8. Configuration

Deprecation configuration is managed via the `XIOSYNC_API_DEPRECATION_CONFIG`
environment variable, which accepts a JSON object mapping endpoint patterns to
deprecation/sunset dates:

```json
{
  "POST /api/v1/legacy-endpoint": {
    "deprecation": "2027-01-01",
    "sunset": "2027-06-01"
  }
}
```

This is enforced by the `VersionGovernanceMiddleware` in the API layer.
