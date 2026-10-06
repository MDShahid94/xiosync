"""Tests for the AI Gateway and Workflow Generator (Phase 6).

Validates:
  - AIGateway: provider selection, auto-detect, generation interface
  - GenerationResult: data structure
  - WorkflowGenerator: prompt building, source cleaning, meta extraction
  - API: provider listing
"""
from __future__ import annotations

import asyncio
import os
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


# ── GenerationResult ──────────────────────────────────────────────────────────

class TestGenerationResult:
    def test_success_result(self):
        from xiosync.subsystems.xioflow.services.ai_gateway import GenerationResult
        r = GenerationResult(text="hello", provider="test", model="m1", success=True)
        assert r.text == "hello"
        assert r.success is True
        d = r.to_dict()
        assert d["provider"] == "test"

    def test_error_result(self):
        from xiosync.subsystems.xioflow.services.ai_gateway import GenerationResult
        r = GenerationResult(success=False, error="timeout", provider="agy")
        assert r.success is False
        assert r.error == "timeout"


# ── AGYProvider ───────────────────────────────────────────────────────────────

class TestAGYProvider:
    def _run(self, coro):
        return asyncio.run(coro)

    def test_not_available(self):
        from xiosync.subsystems.xioflow.services.ai_gateway import AGYProvider
        prov = AGYProvider(agy_bin="/nonexistent/agy")
        result = self._run(prov.generate("test"))
        assert result.success is False
        assert "not found" in result.error

    def test_provider_name(self):
        from xiosync.subsystems.xioflow.services.ai_gateway import AGYProvider
        assert AGYProvider.name == "agy"


# ── GeminiAPIProvider ────────────────────────────────────────────────────────

class TestGeminiAPIProvider:
    def test_no_api_key(self):
        from xiosync.subsystems.xioflow.services.ai_gateway import GeminiAPIProvider
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("GEMINI_API_KEY", None)
            prov = GeminiAPIProvider(api_key="")
            result = asyncio.run(prov.generate("test"))
            assert result.success is False
            assert "not configured" in result.error


# ── OpenAIAPIProvider ────────────────────────────────────────────────────────

class TestOpenAIAPIProvider:
    def test_no_api_key(self):
        from xiosync.subsystems.xioflow.services.ai_gateway import OpenAIAPIProvider
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("OPENAI_API_KEY", None)
            prov = OpenAIAPIProvider(api_key="")
            result = asyncio.run(prov.generate("test"))
            assert result.success is False
            assert "not configured" in result.error


# ── CustomHTTPProvider ───────────────────────────────────────────────────────

class TestCustomHTTPProvider:
    def test_no_url(self):
        from xiosync.subsystems.xioflow.services.ai_gateway import CustomHTTPProvider
        prov = CustomHTTPProvider(url="")
        result = asyncio.run(prov.generate("test"))
        assert result.success is False


# ── Provider Registry ────────────────────────────────────────────────────────

class TestProviderRegistry:
    def test_register_custom(self):
        from xiosync.subsystems.xioflow.services.ai_gateway import (
            register_provider, _PROVIDER_REGISTRY, GenerationProvider, GenerationResult,
        )

        class TestProvider(GenerationProvider):
            name = "test_custom"
            async def generate(self, prompt, **kw):
                return GenerationResult(text="custom", provider=self.name, success=True)

        register_provider("test_custom", TestProvider)
        assert "test_custom" in _PROVIDER_REGISTRY
        # Cleanup
        del _PROVIDER_REGISTRY["test_custom"]

    def test_all_default_providers(self):
        from xiosync.subsystems.xioflow.services.ai_gateway import _PROVIDER_REGISTRY
        assert "agy" in _PROVIDER_REGISTRY
        assert "gemini" in _PROVIDER_REGISTRY
        assert "openai" in _PROVIDER_REGISTRY
        assert "custom" in _PROVIDER_REGISTRY


# ── AIGateway ────────────────────────────────────────────────────────────────

class TestAIGateway:
    def test_explicit_provider_string(self):
        from xiosync.subsystems.xioflow.services.ai_gateway import AIGateway
        gw = AIGateway(provider="agy")
        assert gw.provider_name == "agy"

    def test_unknown_provider_raises(self):
        from xiosync.subsystems.xioflow.services.ai_gateway import AIGateway
        with pytest.raises(ValueError, match="Unknown AI provider"):
            AIGateway(provider="nonexistent")

    def test_explicit_provider_instance(self):
        from xiosync.subsystems.xioflow.services.ai_gateway import (
            AIGateway, AGYProvider,
        )
        prov = AGYProvider()
        gw = AIGateway(provider=prov)
        assert gw.provider_name == "agy"

    def test_auto_detect(self):
        from xiosync.subsystems.xioflow.services.ai_gateway import AIGateway
        # Auto-detect should not crash
        gw = AIGateway()
        assert gw.provider_name  # some provider should be selected


# ── WorkflowGenerator ────────────────────────────────────────────────────────

class TestWorkflowGenerator:
    def test_clean_source_strips_fences(self):
        from xiosync.subsystems.xioflow.services.workflow_generator import WorkflowGenerator
        gen = WorkflowGenerator()
        raw = "```javascript\nexport const meta = {};\n```"
        assert gen._clean_source(raw) == "export const meta = {};"

    def test_clean_source_no_fences(self):
        from xiosync.subsystems.xioflow.services.workflow_generator import WorkflowGenerator
        gen = WorkflowGenerator()
        raw = "export const meta = {};"
        assert gen._clean_source(raw) == "export const meta = {};"

    def test_extract_meta(self):
        from xiosync.subsystems.xioflow.services.workflow_generator import WorkflowGenerator
        gen = WorkflowGenerator()
        source = """
export const meta = {
  name: 'test-flow',
  description: 'A test workflow',
  params: {},
};
"""
        meta = gen._extract_meta(source)
        assert meta["name"] == "test-flow"
        assert meta["description"] == "A test workflow"

    def test_extract_meta_no_meta(self):
        from xiosync.subsystems.xioflow.services.workflow_generator import WorkflowGenerator
        gen = WorkflowGenerator()
        meta = gen._extract_meta("no meta here")
        assert meta == {}

    def test_slugify(self):
        from xiosync.subsystems.xioflow.services.workflow_generator import WorkflowGenerator
        gen = WorkflowGenerator()
        assert gen._slugify("Sign in to Google!") == "sign-in-to-google"
        assert gen._slugify("Book a Flight") == "book-a-flight"


# ── GeneratedWorkflow ────────────────────────────────────────────────────────

class TestGeneratedWorkflow:
    def test_fields(self):
        from xiosync.subsystems.xioflow.services.workflow_generator import GeneratedWorkflow
        gw = GeneratedWorkflow(
            script_ref="generated/test.mjs",
            source_code="export const meta = {};",
            provider="agy",
        )
        assert gw.script_ref == "generated/test.mjs"
        assert gw.saved is False
