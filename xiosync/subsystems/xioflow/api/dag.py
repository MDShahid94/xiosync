"""XIOFLOW DAG management API — deploy declarative graphs and convert scripts.

Endpoints:
  POST /xioflow/dags/deploy    Seed memory nodes from a JSON/YAML DAG spec
  POST /xioflow/dags/convert   Convert a .mjs script to DAG JSON via Gemini
                               Add ?commit=true to also persist as memory nodes
"""
from __future__ import annotations

import asyncio
import logging
import os
import uuid
from pathlib import Path
from typing import Any, cast

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, ConfigDict
from sqlalchemy.orm import Session as OrmSession

from xiosync.domain.context import OrgContext
from xiosync.platform.ids import new_id

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/xioflow/dags", tags=["XIOFLOW DAGs"])

# tools/workflows/ base path — relative to project root
_WORKFLOWS_DIR = Path(__file__).parents[5] / "tools" / "workflows"


def _session(request: Request) -> OrmSession:
    return cast(OrmSession, request.state.org_session)


def _ctx(request: Request) -> OrgContext:
    return cast(OrgContext, request.state.org_context)


# ── Request models ─────────────────────────────────────────────────────────────

class DeployRequest(BaseModel):
    """Declarative DAG graph to seed into memory nodes."""
    dag_domain: str
    dag_root_intent: str
    nodes: list[dict[str, Any]]
    project_id: uuid.UUID | None = None
    context_hash: str = "default"
    # If true, also create a workflow_templates row pointing to this DAG
    register_template: bool = True
    template_name: str | None = None
    model_config = ConfigDict(from_attributes=True)


class ConvertRequest(BaseModel):
    """Convert a .mjs script file to a declarative DAG JSON spec."""
    script_ref: str          # e.g. "google-signin.mjs"
    dag_domain: str | None = None    # override domain (default: infer from script)
    dag_root_intent: str | None = None  # override root intent
    model_config = ConfigDict(from_attributes=True)


# ── Endpoints ──────────────────────────────────────────────────────────────────

@router.post("/deploy", summary="Seed DAG memory nodes from a JSON spec", status_code=201)
async def deploy_dag(request: Request, body: DeployRequest) -> dict[str, Any]:
    """Deploy a declarative workflow graph into xioflow_memory_nodes.

    Each node in the graph is inserted as an XioflowMemoryNode.  Optionally
    creates a workflow_templates row (template_type='xioflow_dag') pointing
    to the root intent so the run dispatcher can execute it.
    """
    session = _session(request)
    ctx = _ctx(request)
    org_id = str(ctx.organization_id)

    from xiosync.subsystems.xioflow.memory.memory_graph import MemoryGraph
    from xiosync.subsystems.xioflow.ingestion.dag_deployer import DAGDeployer

    memory_graph = MemoryGraph(session)
    deployer = DAGDeployer(memory_graph)

    graph_data = {
        "nodes": body.nodes,
        "domain": body.dag_domain,
        "root_intent": body.dag_root_intent,
    }

    try:
        node_ids = await deployer.deploy_from_json(
            graph_data,
            org_id=org_id,
            project_id=str(body.project_id) if body.project_id else None,
            context_hash=body.context_hash,
        )
    except Exception as exc:
        logger.exception("dag_deploy_failed", extra={"org_id": org_id})
        raise HTTPException(status_code=500, detail=f"DAG deploy failed: {exc}") from exc

    template_id: str | None = None
    if body.register_template:
        from xiosync.subsystems.xioflow.services.templates import WorkflowTemplateService
        svc = WorkflowTemplateService(session)
        tmpl_name = body.template_name or f"{body.dag_domain}/{body.dag_root_intent}"
        record = svc.register_template(
            ctx,
            name=tmpl_name,
            template_type="xioflow_dag",
            dag_domain=body.dag_domain,
            dag_root_intent=body.dag_root_intent,
            project_id=body.project_id,
        )
        template_id = str(record.id)

    session.commit()
    logger.info(
        "dag_deployed",
        extra={"org_id": org_id, "nodes": len(node_ids), "template_id": template_id},
    )
    return {
        "node_ids": node_ids,
        "node_count": len(node_ids),
        "dag_domain": body.dag_domain,
        "dag_root_intent": body.dag_root_intent,
        "template_id": template_id,
    }


@router.post("/convert", summary="Convert a .mjs script to a DAG spec via Gemini")
async def convert_script(
    request: Request,
    body: ConvertRequest,
    commit: bool = False,
) -> dict[str, Any]:
    """LLM-powered conversion of an imperative .mjs workflow to a declarative DAG.

    Reads the script source, sends it to Gemini with structured output schema,
    and returns the generated DAG JSON.

    Add ?commit=true to also persist the result via /dags/deploy.
    The original .mjs template is preserved — both coexist and can be compared.
    """
    # Locate the script
    script_path = _WORKFLOWS_DIR / body.script_ref
    if not script_path.exists():
        raise HTTPException(
            status_code=404,
            detail=f"Script not found: {body.script_ref} (looked in {_WORKFLOWS_DIR})",
        )

    script_source = script_path.read_text(encoding="utf-8")

    # Infer domain from script name if not provided
    dag_domain = body.dag_domain or body.script_ref.replace(".mjs", "").split("-")[0]
    dag_root_intent = body.dag_root_intent or (
        body.script_ref.replace(".mjs", "").replace("-", "_")
    )

    # Build Gemini prompt
    _DAG_SCHEMA = """{
  "dag_domain": "<FQDN of the target site>",
  "dag_root_intent": "<snake_case entry intent>",
  "nodes": [
    {
      "domain": "<FQDN>",
      "url": "<page URL where action occurs>",
      "intent": "<unique snake_case intent name>",
      "action_type": "<navigate|click|type|extract_data|wait|scroll_down|delay|script|done>",
      "face_value": {"description": "<human readable label>", "text": "<visible text>"},
      "place_value": {"css": "<CSS selector>", "aria_label": "<aria>", "test_id": "<data-testid>", "xpath": "<XPath>"},
      "action_params": {"<key>": "<value or {{param_name}} template>"},
      "output_var": "<optional output variable name>",
      "previous_node_id": "<intent of predecessor node or null>",
      "execution_mode": "sequential"
    }
  ]
}"""

    prompt = f"""You are an expert browser automation engineer converting an imperative Playwright/Patchright .mjs workflow into a declarative DAG specification.

SCRIPT SOURCE:
```javascript
{script_source}
```

OUTPUT INSTRUCTIONS:
- Identify each distinct browser action as a separate node
- Map action types: page.goto → navigate, page.click/locator.click → click, page.fill/type → type, page.waitForSelector → wait, await page.evaluate(scroll) → scroll_down
- For sensitive params (password, token, secret), use {{{{param_name}}}} template syntax
- Preserve the sequential ordering via previous_node_id chains
- The last node must have action_type="done"
- For stealth/TOTP steps that cannot be mapped, use action_type="script" with the same script_ref

Return ONLY valid JSON matching this exact schema (no markdown, no explanation):
{_DAG_SCHEMA}
"""

    # Call Gemini
    try:
        import google.generativeai as genai  # type: ignore[import]
        api_key = os.environ.get("GEMINI_API_KEY", "")
        if not api_key:
            raise HTTPException(status_code=503, detail="GEMINI_API_KEY not configured")

        genai.configure(api_key=api_key)
        model = genai.GenerativeModel(
            model_name="gemini-1.5-flash",
            generation_config={"response_mime_type": "application/json"},
        )
        response = model.generate_content(prompt)
        import json
        dag_json = json.loads(response.text)
    except HTTPException:
        raise
    except Exception as exc:
        logger.exception("dag_convert_gemini_failed", extra={"script_ref": body.script_ref})
        raise HTTPException(status_code=500, detail=f"Gemini conversion failed: {exc}") from exc

    # Override domain/intent if explicitly requested
    dag_json["dag_domain"] = dag_domain
    dag_json["dag_root_intent"] = dag_root_intent

    result: dict[str, Any] = {
        "script_ref": body.script_ref,
        "dag_domain": dag_domain,
        "dag_root_intent": dag_root_intent,
        "node_count": len(dag_json.get("nodes", [])),
        "dag": dag_json,
        "committed": False,
    }

    # Persist if requested
    if commit:
        ctx = _ctx(request)
        session = _session(request)
        from xiosync.subsystems.xioflow.memory.memory_graph import MemoryGraph
        from xiosync.subsystems.xioflow.ingestion.dag_deployer import DAGDeployer
        from xiosync.subsystems.xioflow.services.templates import WorkflowTemplateService

        memory_graph = MemoryGraph(session)
        deployer = DAGDeployer(memory_graph)
        node_ids = await deployer.deploy_from_json(
            dag_json, org_id=str(ctx.organization_id)
        )

        svc = WorkflowTemplateService(session)
        tmpl = svc.register_template(
            ctx,
            name=f"{dag_json['dag_domain']}/{dag_json['dag_root_intent']} (converted)",
            template_type="xioflow_dag",
            dag_domain=dag_json["dag_domain"],
            dag_root_intent=dag_json["dag_root_intent"],
            description=f"Auto-converted from {body.script_ref}",
        )
        session.commit()

        result["committed"] = True
        result["node_ids"] = node_ids
        result["template_id"] = str(tmpl.id)
        logger.info(
            "dag_converted_and_committed",
            extra={"script_ref": body.script_ref, "nodes": len(node_ids)},
        )

    return result
