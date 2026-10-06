"""workflow_generator.py — AI-powered workflow script generator.

Takes a human-language description and generates a structured
WorkflowDSL script (.mjs) that can be executed by ScriptRunner.

Uses ``AIGateway`` for provider-agnostic AI generation — any configured
provider (agy, gemini, openai, custom) is automatically used.

The generated scripts use the standard ``ctx.step()`` / ``ctx.page`` API
and are fully compatible with PageProxy trace mode.
"""
from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

_WORKFLOWS_DIR = Path(
    os.environ.get(
        "XIOSYNC_WORKFLOWS_DIR",
        str(Path(__file__).parent.parent.parent.parent.parent / "tools" / "workflows"),
    )
)
_GENERATED_DIR = _WORKFLOWS_DIR / "generated"

# ── Reference template for AI context ────────────────────────────────────────

_SYSTEM_PROMPT = """\
You are a XIOSYNC workflow engineer. You generate production-grade .mjs workflow
scripts that run inside XIOSYNC's ScriptRunner (Node.js subprocess).

ARCHITECTURE RULES:
1. Every script MUST export `meta` and `run(ctx, params)`.
2. Use `ctx.step(name, async () => { ... })` for each logical step.
3. Browser interactions use xiorun_agent HTTP endpoints, NOT direct Playwright.
4. Use `ctx.setResult({...})` at the end to emit the final result.
5. Use `vault://` references for secrets in meta.params descriptions.
6. Include error handling: wrap risky steps in try/catch.
7. For steps that might fail, add HITL fallback comments.
8. HTTP calls to xiorun_agent use: POST xiorun_url + endpoint.
9. Use ctx.log(msg) for structured logging.

SCRIPT STRUCTURE:
```javascript
export const meta = {
  name: '<kebab-case-name>',
  description: '<clear description>',
  params: { <param>: '<description>', ... },
};

// Node.js stdlib only
import { ... } from 'node:...';

export async function run(ctx, params) {
  const { param1, param2 } = params;
  
  await ctx.step('step_name', async () => {
    // ... action logic
  });
  
  ctx.setResult({ status: 'success', ... });
}
```

HTTP CALL PATTERN (to xiorun_agent):
```javascript
const resp = await fetch(xiorun_url + '/endpoint', {
  method: 'POST',
  headers: { 'Content-Type': 'application/json' },
  body: JSON.stringify({ key: 'value' }),
});
const data = await resp.json();
```

OUTPUT: Return ONLY the .mjs file content. No markdown fences, no explanation.
"""


# ── Data classes ──────────────────────────────────────────────────────────────

@dataclass
class GeneratedWorkflow:
    """Result of AI workflow generation."""
    script_ref: str                    # e.g. "generated/book-flight.mjs"
    source_code: str                   # the .mjs content
    meta: dict[str, Any] = field(default_factory=dict)
    provider: str = ""                 # which AI provider generated it
    model: str = ""
    saved: bool = False


# ── WorkflowGenerator ────────────────────────────────────────────────────────

class WorkflowGenerator:
    """Generates .mjs workflow scripts from human descriptions.

    Uses ``AIGateway`` for provider-agnostic generation. The gateway
    auto-detects the best available provider (agy CLI preferred).

    Usage::

        gen = WorkflowGenerator()
        result = await gen.generate(
            description="Sign in to Slack with Google SSO",
            target_domain="slack.com",
        )
        if result.source_code:
            print(result.source_code)
    """

    def __init__(
        self,
        provider: str | None = None,
        workflows_dir: Path | None = None,
    ) -> None:
        self._provider_name = provider
        self._workflows_dir = workflows_dir or _WORKFLOWS_DIR
        self._generated_dir = self._workflows_dir / "generated"

    async def generate(
        self,
        description: str,
        target_domain: str = "",
        params: dict[str, str] | None = None,
        reference_script: str | None = None,
        timeout: int = 120,
    ) -> GeneratedWorkflow:
        """Generate a workflow script from a natural language description.

        Args:
            description: Human description, e.g. "Sign in to Slack with Google SSO"
            target_domain: Primary domain, e.g. "slack.com"
            params: Expected input parameters {name: description}
            reference_script: Name of existing script to use as reference
            timeout: Generation timeout in seconds

        Returns:
            GeneratedWorkflow with the .mjs source code and metadata.
        """
        from xiosync.subsystems.xioai.gateway import AIGateway

        prompt = self._build_prompt(description, target_domain, params, reference_script)

        gw = AIGateway(provider=self._provider_name)
        result = await gw.generate(
            prompt=prompt,
            system=_SYSTEM_PROMPT,
            output_format="text",
            temperature=0.2,
            max_tokens=8192,
            timeout=timeout,
        )

        if not result.success:
            logger.warning(
                "workflow_generator.generation_failed",
                extra={"error": result.error, "provider": result.provider},
            )
            return GeneratedWorkflow(
                script_ref="",
                source_code="",
                meta={"error": result.error},
                provider=result.provider,
            )

        source_code = self._clean_source(result.text)
        meta = self._extract_meta(source_code)
        script_name = meta.get("name", self._slugify(description))
        script_ref = f"generated/{script_name}.mjs"

        return GeneratedWorkflow(
            script_ref=script_ref,
            source_code=source_code,
            meta=meta,
            provider=result.provider,
            model=result.model,
        )

    async def generate_and_save(
        self,
        description: str,
        target_domain: str = "",
        params: dict[str, str] | None = None,
        reference_script: str | None = None,
        timeout: int = 120,
    ) -> GeneratedWorkflow:
        """Generate and save the script to the workflows/generated/ directory."""
        result = await self.generate(
            description=description,
            target_domain=target_domain,
            params=params,
            reference_script=reference_script,
            timeout=timeout,
        )

        if result.source_code:
            self._generated_dir.mkdir(parents=True, exist_ok=True)
            target = self._workflows_dir / result.script_ref
            target.write_text(result.source_code, encoding="utf-8")
            result.saved = True
            logger.info(
                "workflow_generator.saved",
                extra={"path": str(target), "script_ref": result.script_ref},
            )

        return result

    # ── Internal helpers ──────────────────────────────────────────────

    def _build_prompt(
        self,
        description: str,
        target_domain: str,
        params: dict[str, str] | None,
        reference_script: str | None,
    ) -> str:
        """Build the generation prompt with context and examples."""
        parts = [
            f"TASK: Generate a XIOSYNC workflow script (.mjs) for:\n",
            f"DESCRIPTION: {description}\n",
        ]
        if target_domain:
            parts.append(f"TARGET DOMAIN: {target_domain}\n")
        if params:
            parts.append(f"EXPECTED PARAMS: {json.dumps(params, indent=2)}\n")

        # Load reference script for in-context example
        ref_name = reference_script or "google-signin.mjs"
        ref_path = self._workflows_dir / ref_name
        if ref_path.exists():
            ref_source = ref_path.read_text(encoding="utf-8")
            # Truncate to first 200 lines for context window efficiency
            ref_lines = ref_source.split("\n")[:200]
            parts.append(
                f"\nREFERENCE SCRIPT ({ref_name}, first 200 lines):\n"
                f"```javascript\n{'chr(10)'.join(ref_lines)}\n```\n"
            )

        parts.append(
            "\nOUTPUT: Return ONLY the complete .mjs file content. "
            "No markdown fences, no explanation, no comments outside the script."
        )
        return "\n".join(parts)

    def _clean_source(self, raw: str) -> str:
        """Strip markdown fences and leading/trailing whitespace."""
        text = raw.strip()
        # Remove markdown code fences if present
        if text.startswith("```"):
            lines = text.split("\n")
            # Remove first line (```javascript) and last line (```)
            if lines[-1].strip() == "```":
                lines = lines[1:-1]
            else:
                lines = lines[1:]
            text = "\n".join(lines)
        return text.strip()

    def _extract_meta(self, source: str) -> dict[str, Any]:
        """Extract the meta object from the generated source code."""
        import re
        # Look for: export const meta = { ... };
        match = re.search(
            r"export\s+const\s+meta\s*=\s*\{([^}]+(?:\{[^}]*\}[^}]*)*)\}",
            source, re.DOTALL,
        )
        if not match:
            return {}

        try:
            # Try to parse the meta block (it's JS object notation, not strict JSON)
            block = match.group(0)
            # Extract name
            name_match = re.search(r"name:\s*['\"]([^'\"]+)['\"]", block)
            desc_match = re.search(r"description:\s*['\"]([^'\"]+)['\"]", block)
            return {
                "name": name_match.group(1) if name_match else "",
                "description": desc_match.group(1) if desc_match else "",
            }
        except Exception:
            return {}

    def _slugify(self, text: str) -> str:
        """Convert description to a kebab-case slug."""
        import re
        slug = text.lower().strip()
        slug = re.sub(r"[^a-z0-9\s-]", "", slug)
        slug = re.sub(r"[\s]+", "-", slug)
        return slug[:50]
