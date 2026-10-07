"""Unit tests for the XIOAI subsystem — unified AI gateway."""

from __future__ import annotations

import os
from unittest.mock import MagicMock, patch

import pytest


@pytest.fixture(autouse=True)
def _isolate_provider_registry():
    """Snapshot and restore _PROVIDER_REGISTRY around every test.

    test_ai_gateway.py registers keys like 'test_custom' into the OLD
    xioflow ai_gateway registry, but since _PROVIDER_REGISTRY in the new
    xioai.gateway module is a module-level dict, any mutation from other
    test files running first bleeds in. This fixture prevents that.
    """
    from xiosync.subsystems.xioai import gateway as gw_mod

    snapshot = dict(gw_mod._PROVIDER_REGISTRY)
    yield
    gw_mod._PROVIDER_REGISTRY.clear()
    gw_mod._PROVIDER_REGISTRY.update(snapshot)


# ── Provider base ──────────────────────────────────────────────────────────────


class TestGenerationResult:
    def test_creation(self):
        from xiosync.subsystems.xioai.providers.base import GenerationResult

        r = GenerationResult(text="hello", provider="test", model="m1", success=True)
        assert r.text == "hello"
        assert r.provider == "test"
        assert r.model == "m1"
        assert r.success is True
        assert r.error is None

    def test_failure(self):
        from xiosync.subsystems.xioai.providers.base import GenerationResult

        r = GenerationResult(success=False, error="boom", provider="test")
        assert r.success is False
        assert r.error == "boom"
        assert r.text == ""


class TestGenerationProvider:
    def test_abc(self):
        from xiosync.subsystems.xioai.providers.base import GenerationProvider

        # Can't instantiate abstract class
        with pytest.raises(TypeError):
            GenerationProvider()


# ── Provider registry & gateway ────────────────────────────────────────────────


class TestProviderRegistry:
    def test_registry_has_providers(self):
        from xiosync.subsystems.xioai.gateway import _PROVIDER_REGISTRY

        assert "agy_local" in _PROVIDER_REGISTRY
        assert "agy_remote" in _PROVIDER_REGISTRY
        assert "gemini" in _PROVIDER_REGISTRY
        assert "openai" in _PROVIDER_REGISTRY
        assert "custom" in _PROVIDER_REGISTRY
        assert len(_PROVIDER_REGISTRY) >= 5

    def test_register_custom_provider(self):
        from xiosync.subsystems.xioai.gateway import _PROVIDER_REGISTRY, register_provider
        from xiosync.subsystems.xioai.providers.base import GenerationProvider, GenerationResult

        class DummyProvider(GenerationProvider):
            @property
            def name(self) -> str:
                return "dummy"

            @classmethod
            def is_available(cls) -> bool:
                return True

            async def generate(self, prompt, **kwargs):
                return GenerationResult(text="dummy", provider="dummy", model="d", success=True)

        register_provider("dummy", DummyProvider)
        assert "dummy" in _PROVIDER_REGISTRY
        # Clean up
        del _PROVIDER_REGISTRY["dummy"]


class TestAIGateway:
    def test_available_providers(self):
        from xiosync.subsystems.xioai.gateway import AIGateway

        avail = AIGateway.available_providers()
        assert isinstance(avail, dict)
        assert "agy_local" in avail
        assert "gemini" in avail

    def test_explicit_provider(self):
        from xiosync.subsystems.xioai.gateway import AIGateway

        with patch.dict(os.environ, {"GEMINI_API_KEY": "test-key"}):
            gw = AIGateway(provider="gemini")
            assert gw.provider_name == "gemini"

    def test_env_provider(self):
        from xiosync.subsystems.xioai.gateway import AIGateway

        with patch.dict(os.environ, {"XIOAI_PROVIDER": "gemini", "GEMINI_API_KEY": "k"}):
            gw = AIGateway()
            assert gw.provider_name == "gemini"


# ── AGYLocalProvider ───────────────────────────────────────────────────────────


class TestAGYLocalProvider:
    def test_is_available(self):
        from xiosync.subsystems.xioai.providers.agy_local import AGYLocalProvider

        # On the Mac Mini, agy should be available
        result = AGYLocalProvider.is_available()
        assert isinstance(result, bool)

    def test_is_available_with_env(self):
        from xiosync.subsystems.xioai.providers.agy_local import AGYLocalProvider

        with patch.dict(os.environ, {"XIOAI_AGY_BIN": "/nonexistent/agy"}):
            assert AGYLocalProvider.is_available() is True  # env var set = available

    @pytest.mark.asyncio
    async def test_generate_not_available(self):
        import xiosync.subsystems.xioai.providers.agy_local as _agy_mod
        from xiosync.subsystems.xioai.providers.agy_local import AGYLocalProvider

        orig_socket = _agy_mod.AGY_SIDECAR_SOCKET
        _agy_mod.AGY_SIDECAR_SOCKET = "/tmp/nonexistent-xioai-test.sock"
        try:
            with patch("shutil.which", return_value=None), patch.dict(os.environ, {}, clear=True):
                provider = AGYLocalProvider()
                result = await provider.generate("test")
                assert result.success is False
                assert "not found" in result.error.lower()
        finally:
            _agy_mod.AGY_SIDECAR_SOCKET = orig_socket


# ── AGYRemoteProvider ──────────────────────────────────────────────────────────


class TestAGYRemoteProvider:
    def test_not_available_without_env(self):
        from xiosync.subsystems.xioai.providers.agy_remote import AGYRemoteProvider

        with patch.dict(os.environ, {}, clear=True):
            assert AGYRemoteProvider.is_available() is False

    def test_available_with_env(self):
        from xiosync.subsystems.xioai.providers.agy_remote import AGYRemoteProvider

        mock_resp = MagicMock()
        mock_resp.status_code = 200
        with (
            patch.dict(os.environ, {"XIOAI_REMOTE_URL": "http://localhost:9300"}),
            patch("httpx.get", return_value=mock_resp),
        ):
            assert AGYRemoteProvider.is_available() is True


# ── GeminiProvider ─────────────────────────────────────────────────────────────


class TestGeminiProvider:
    def test_not_available_without_key(self):
        from xiosync.subsystems.xioai.providers.gemini import GeminiProvider

        with patch.dict(os.environ, {}, clear=True):
            assert GeminiProvider.is_available() is False

    def test_available_with_key(self):
        from xiosync.subsystems.xioai.providers.gemini import GeminiProvider

        with patch.dict(os.environ, {"GEMINI_API_KEY": "test-key"}):
            assert GeminiProvider.is_available() is True


# ── OpenAIProvider ─────────────────────────────────────────────────────────────


class TestOpenAIProvider:
    def test_not_available_without_key(self):
        from xiosync.subsystems.xioai.providers.openai_provider import OpenAIProvider

        with patch.dict(os.environ, {}, clear=True):
            assert OpenAIProvider.is_available() is False

    def test_available_with_key(self):
        from xiosync.subsystems.xioai.providers.openai_provider import OpenAIProvider

        with patch.dict(os.environ, {"OPENAI_API_KEY": "sk-test"}):
            assert OpenAIProvider.is_available() is True


# ── CustomHTTPProvider ─────────────────────────────────────────────────────────


class TestCustomHTTPProvider:
    def test_not_available_without_url(self):
        from xiosync.subsystems.xioai.providers.custom_http import CustomHTTPProvider

        with patch.dict(os.environ, {}, clear=True):
            assert CustomHTTPProvider.is_available() is False

    def test_available_with_url(self):
        from xiosync.subsystems.xioai.providers.custom_http import CustomHTTPProvider

        with patch.dict(os.environ, {"XIOAI_CUSTOM_URL": "http://example.com/ai"}):
            assert CustomHTTPProvider.is_available() is True


# ── Integration: gateway generate with mocked provider ─────────────────────────


class TestGatewayIntegration:
    @pytest.mark.asyncio
    async def test_generate_dispatches_to_provider(self):
        from xiosync.subsystems.xioai.gateway import (
            _PROVIDER_REGISTRY,
            AIGateway,
            register_provider,
        )
        from xiosync.subsystems.xioai.providers.base import GenerationProvider, GenerationResult

        class MockProvider(GenerationProvider):
            @property
            def name(self) -> str:
                return "mock"

            @classmethod
            def is_available(cls) -> bool:
                return True

            async def generate(self, prompt, **kwargs):
                return GenerationResult(
                    text=f"mocked:{prompt}", provider="mock", model="m", success=True
                )

        register_provider("mock", MockProvider)
        try:
            gw = AIGateway(provider="mock")
            result = await gw.generate("hello")
            assert result.success is True
            assert result.text == "mocked:hello"
            assert result.provider == "mock"
        finally:
            del _PROVIDER_REGISTRY["mock"]

    @pytest.mark.asyncio
    async def test_generate_handles_provider_failure(self):
        from xiosync.subsystems.xioai.gateway import (
            _PROVIDER_REGISTRY,
            AIGateway,
            register_provider,
        )
        from xiosync.subsystems.xioai.providers.base import GenerationProvider

        class FailProvider(GenerationProvider):
            @property
            def name(self) -> str:
                return "fail"

            @classmethod
            def is_available(cls) -> bool:
                return True

            async def generate(self, prompt, **kwargs):
                raise RuntimeError("provider crashed")

        register_provider("fail", FailProvider)
        try:
            gw = AIGateway(provider="fail")
            result = await gw.generate("hello")
            assert result.success is False
            assert "provider crashed" in result.error
        finally:
            del _PROVIDER_REGISTRY["fail"]
