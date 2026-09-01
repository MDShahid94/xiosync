#!/usr/bin/env python3
"""Generate client SDK stubs from the XIOSYNC OpenAPI specification.

Supports generating TypeScript, Python, and Go client libraries using
openapi-generator-cli (Java) or openapi-typescript (Node.js).

Usage::

    # TypeScript (for frontend — uses openapi-typescript, no Java needed)
    python tools/generate_sdk.py --lang typescript

    # Python (uses openapi-generator-cli — requires Java 11+)
    python tools/generate_sdk.py --lang python

    # Go
    python tools/generate_sdk.py --lang go

    # All languages
    python tools/generate_sdk.py --lang all

Prerequisites::

    # For TypeScript:
    npm install -g openapi-typescript

    # For Python/Go:
    npm install -g @openapitools/openapi-generator-cli
    # or: brew install openapi-generator
"""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
OPENAPI_SPEC = PROJECT_ROOT / "openapi.json"
SDK_OUTPUT = PROJECT_ROOT / "sdk"


def _check_spec() -> None:
    """Ensure openapi.json exists; export it if not."""
    if not OPENAPI_SPEC.exists():
        print("openapi.json not found — exporting...")
        subprocess.run(
            [sys.executable, str(PROJECT_ROOT / "tools" / "export_openapi.py")],
            check=True,
        )


def _generate_typescript() -> None:
    """Generate TypeScript types using openapi-typescript."""
    out_dir = SDK_OUTPUT / "typescript"
    out_dir.mkdir(parents=True, exist_ok=True)

    # Check if openapi-typescript is available.
    if shutil.which("openapi-typescript"):
        subprocess.run(
            ["openapi-typescript", str(OPENAPI_SPEC), "-o", str(out_dir / "schema.d.ts")],
            check=True,
        )
        print(f"✓ TypeScript SDK generated at {out_dir}/schema.d.ts")
    else:
        # Fallback: generate a basic type file from the spec.
        spec = json.loads(OPENAPI_SPEC.read_text())
        schemas = spec.get("components", {}).get("schemas", {})
        lines = [
            "// Auto-generated from openapi.json — do not edit manually",
            f"// Generated from {len(schemas)} schemas",
            "",
            "export type paths = Record<string, unknown>;",
            "",
        ]
        for name in sorted(schemas):
            lines.append(f"export type {name} = Record<string, unknown>;")
        (out_dir / "schema.d.ts").write_text("\n".join(lines) + "\n")
        print(f"✓ TypeScript SDK (basic) generated at {out_dir}/schema.d.ts")
        print("  Install openapi-typescript for full type generation")


def _generate_python() -> None:
    """Generate Python client using openapi-generator-cli."""
    out_dir = SDK_OUTPUT / "python"
    gen = shutil.which("openapi-generator-cli") or shutil.which("openapi-generator")
    if not gen:
        print("✗ openapi-generator not found. Install with:")
        print("  npm install -g @openapitools/openapi-generator-cli")
        print("  # or: brew install openapi-generator")
        return

    subprocess.run(
        [
            gen, "generate",
            "-i", str(OPENAPI_SPEC),
            "-g", "python",
            "-o", str(out_dir),
            "--package-name", "xiosync_client",
            "--additional-properties", "projectName=xiosync-client,packageVersion=0.1.0",
        ],
        check=True,
    )
    print(f"✓ Python SDK generated at {out_dir}")


def _generate_go() -> None:
    """Generate Go client using openapi-generator-cli."""
    out_dir = SDK_OUTPUT / "go"
    gen = shutil.which("openapi-generator-cli") or shutil.which("openapi-generator")
    if not gen:
        print("✗ openapi-generator not found. Install with:")
        print("  npm install -g @openapitools/openapi-generator-cli")
        return

    subprocess.run(
        [
            gen, "generate",
            "-i", str(OPENAPI_SPEC),
            "-g", "go",
            "-o", str(out_dir),
            "--package-name", "xiosync",
            "--additional-properties", "packageVersion=0.1.0",
        ],
        check=True,
    )
    print(f"✓ Go SDK generated at {out_dir}")


def main() -> None:
    parser = argparse.ArgumentParser(description="Generate XIOSYNC client SDKs")
    parser.add_argument(
        "--lang",
        choices=["typescript", "python", "go", "all"],
        default="typescript",
        help="Language to generate (default: typescript)",
    )
    args = parser.parse_args()

    _check_spec()

    generators = {
        "typescript": _generate_typescript,
        "python": _generate_python,
        "go": _generate_go,
    }

    if args.lang == "all":
        for gen in generators.values():
            gen()
    else:
        generators[args.lang]()


if __name__ == "__main__":
    main()
