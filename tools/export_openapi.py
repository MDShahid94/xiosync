#!/usr/bin/env python3
"""Export the XIOSYNC OpenAPI specification to openapi.json.

Usage::

    python tools/export_openapi.py           # writes openapi.json to repo root
    python tools/export_openapi.py -o out.json  # writes to custom path

Used by CI Gate 6 to detect API contract drift.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

# Ensure the project root is on sys.path.
project_root = str(Path(__file__).resolve().parent.parent)
if project_root not in sys.path:
    sys.path.insert(0, project_root)


def export_openapi(output_path: str = "openapi.json") -> None:
    """Generate and write the OpenAPI spec from the FastAPI app."""
    # Set minimal required env vars for app construction.
    os.environ.setdefault("XIOSYNC_AUTH_SECRET", "export-dummy-secret")
    os.environ.setdefault("XIOSYNC_ENVIRONMENT", "testing")
    os.environ.setdefault("XIOSYNC_CORS_ALLOWED_ORIGINS", "http://localhost")
    os.environ.setdefault("DATABASE_URL", "postgresql+psycopg://x:x@localhost/x")

    from xiosync.api.app import create_production_app

    app = create_production_app()
    spec = app.openapi()

    output = Path(output_path)
    output.write_text(json.dumps(spec, indent=2, default=str) + "\n")

    endpoint_count = sum(
        len(methods) for path_item in spec.get("paths", {}).values()
        for methods in [path_item] if isinstance(path_item, dict)
    )
    print(f"✓ OpenAPI spec exported to {output} ({len(spec.get('paths', {}))} paths)")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Export XIOSYNC OpenAPI spec")
    parser.add_argument("-o", "--output", default="openapi.json", help="Output file path")
    args = parser.parse_args()
    export_openapi(args.output)
