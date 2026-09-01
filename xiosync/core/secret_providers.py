"""Secret provider adapters — resolve secret references to actual values.

Each adapter resolves a ``SecretRefRecord`` into its plaintext value by
contacting the appropriate backend. The resolver is used at the worker
side when tasks request their secrets via ``GET /execution/tasks/{id}/secrets``.

Adapters are registered in a provider registry; the ``resolve()`` function
dispatches to the correct adapter based on ``secret.provider``.

Per the user's universality directive, providers are pluggable — organizations
choose their own secret backend (env, Vault, AWS, GCP, Azure, etc.).
"""

from __future__ import annotations

import logging
import os
from abc import ABC, abstractmethod
from typing import Any

__all__ = [
    "SecretProvider",
    "EnvProvider",
    "InlineProvider",
    "VaultProvider",
    "AwsSmProvider",
    "GcpSmProvider",
    "AzureKvProvider",
    "resolve",
    "register_provider",
    "get_provider",
]

logger = logging.getLogger(__name__)


class SecretResolutionError(RuntimeError):
    """Raised when a secret value cannot be resolved."""


class SecretProvider(ABC):
    """Base class for secret provider adapters."""

    @abstractmethod
    def resolve(self, ref_config: dict[str, Any]) -> str:
        """Resolve a secret reference config into its plaintext value.

        Args:
            ref_config: Provider-specific configuration (e.g., ``{"key": "MY_VAR"}``
                for env, ``{"path": "secret/data/api-key", "field": "value"}``
                for Vault).

        Returns:
            The plaintext secret value.

        Raises:
            SecretResolutionError: If the secret cannot be resolved.
        """
        ...


# ── Built-in Providers ───────────────────────────────────────────────────────


class EnvProvider(SecretProvider):
    """Resolve secrets from environment variables.

    Config: ``{"key": "ENV_VAR_NAME"}``
    """

    def resolve(self, ref_config: dict[str, Any]) -> str:
        key = ref_config.get("key")
        if not isinstance(key, str):
            raise SecretResolutionError("env provider requires 'key' in ref_config")
        value = os.environ.get(key)
        if value is None:
            raise SecretResolutionError(f"environment variable {key!r} not set")
        return value


class InlineProvider(SecretProvider):
    """Resolve secrets from inline config (for dev/testing only).

    Config: ``{"value": "the-secret-value"}``

    .. warning:: NOT recommended for production — secrets are stored in the DB.
    """

    def resolve(self, ref_config: dict[str, Any]) -> str:
        value = ref_config.get("value")
        if not isinstance(value, str):
            raise SecretResolutionError("inline provider requires 'value' in ref_config")
        return value


class VaultProvider(SecretProvider):
    """Resolve secrets from HashiCorp Vault.

    Config: ``{"path": "secret/data/my-key", "field": "value", "mount": "secret"}``

    Requires ``VAULT_ADDR`` and ``VAULT_TOKEN`` (or other auth method)
    environment variables.
    """

    def resolve(self, ref_config: dict[str, Any]) -> str:
        path = ref_config.get("path")
        field = ref_config.get("field", "value")
        if not isinstance(path, str):
            raise SecretResolutionError("vault provider requires 'path' in ref_config")

        try:
            import hvac  # type: ignore[import-untyped]
        except ImportError:
            raise SecretResolutionError(
                "vault provider requires 'hvac' package — "
                "install with: pip install hvac"
            )

        addr = os.environ.get("VAULT_ADDR")
        token = os.environ.get("VAULT_TOKEN")
        if not addr:
            raise SecretResolutionError("VAULT_ADDR environment variable not set")

        client = hvac.Client(url=addr, token=token)
        try:
            response = client.secrets.kv.v2.read_secret_version(path=path)
            data = response["data"]["data"]
            if field not in data:
                raise SecretResolutionError(
                    f"field {field!r} not found in Vault secret at {path!r}"
                )
            return str(data[field])
        except Exception as exc:
            raise SecretResolutionError(f"Vault resolution failed: {exc}") from exc


class AwsSmProvider(SecretProvider):
    """Resolve secrets from AWS Secrets Manager.

    Config: ``{"secret_id": "my-secret", "version_stage": "AWSCURRENT"}``

    Requires AWS credentials (env vars, instance profile, etc.).
    """

    def resolve(self, ref_config: dict[str, Any]) -> str:
        secret_id = ref_config.get("secret_id")
        if not isinstance(secret_id, str):
            raise SecretResolutionError("aws-sm provider requires 'secret_id' in ref_config")

        try:
            import boto3  # type: ignore[import-not-found]
        except ImportError:
            raise SecretResolutionError(
                "aws-sm provider requires 'boto3' package — "
                "install with: pip install boto3"
            )

        region = ref_config.get("region", os.environ.get("AWS_DEFAULT_REGION", "us-east-1"))
        version = ref_config.get("version_stage", "AWSCURRENT")

        try:
            client = boto3.client("secretsmanager", region_name=region)
            response = client.get_secret_value(
                SecretId=secret_id, VersionStage=version
            )
            return str(response["SecretString"])
        except Exception as exc:
            raise SecretResolutionError(f"AWS SM resolution failed: {exc}") from exc


class GcpSmProvider(SecretProvider):
    """Resolve secrets from Google Cloud Secret Manager.

    Config: ``{"project": "my-project", "secret_id": "my-secret", "version": "latest"}``

    Requires GCP credentials (ADC, service account, etc.).
    """

    def resolve(self, ref_config: dict[str, Any]) -> str:
        project = ref_config.get("project")
        secret_id = ref_config.get("secret_id")
        version = ref_config.get("version", "latest")

        if not isinstance(project, str) or not isinstance(secret_id, str):
            raise SecretResolutionError(
                "gcp-sm provider requires 'project' and 'secret_id' in ref_config"
            )

        try:
            from google.cloud import secretmanager  # type: ignore[import-not-found]
        except ImportError:
            raise SecretResolutionError(
                "gcp-sm provider requires 'google-cloud-secret-manager' package — "
                "install with: pip install google-cloud-secret-manager"
            )

        try:
            client = secretmanager.SecretManagerServiceClient()
            name = f"projects/{project}/secrets/{secret_id}/versions/{version}"
            response = client.access_secret_version(name=name)
            return str(response.payload.data.decode("utf-8"))
        except Exception as exc:
            raise SecretResolutionError(f"GCP SM resolution failed: {exc}") from exc


class AzureKvProvider(SecretProvider):
    """Resolve secrets from Azure Key Vault.

    Config: ``{"vault_url": "https://myvault.vault.azure.net", "secret_name": "my-secret"}``

    Requires Azure credentials (DefaultAzureCredential).
    """

    def resolve(self, ref_config: dict[str, Any]) -> str:
        vault_url = ref_config.get("vault_url")
        secret_name = ref_config.get("secret_name")

        if not isinstance(vault_url, str) or not isinstance(secret_name, str):
            raise SecretResolutionError(
                "azure-kv provider requires 'vault_url' and 'secret_name' in ref_config"
            )

        try:
            from azure.identity import DefaultAzureCredential  # type: ignore[import-not-found]
            from azure.keyvault.secrets import SecretClient  # type: ignore[import-not-found]
        except ImportError:
            raise SecretResolutionError(
                "azure-kv provider requires 'azure-identity' and "
                "'azure-keyvault-secrets' packages"
            )

        try:
            credential = DefaultAzureCredential()
            client = SecretClient(vault_url=vault_url, credential=credential)
            secret = client.get_secret(secret_name)
            if secret.value is None:
                raise SecretResolutionError(f"Azure KV secret {secret_name!r} has no value")
            return str(secret.value)
        except Exception as exc:
            raise SecretResolutionError(f"Azure KV resolution failed: {exc}") from exc


class CustomProvider(SecretProvider):
    """Placeholder for organization-defined custom providers.

    Config: ``{"resolver": "my_module.resolve_fn", ...}``

    Organizations can register their own providers by implementing a callable
    that takes ``ref_config`` and returns a string.
    """

    def resolve(self, ref_config: dict[str, Any]) -> str:
        resolver_path = ref_config.get("resolver")
        if not isinstance(resolver_path, str):
            raise SecretResolutionError(
                "custom provider requires 'resolver' in ref_config "
                "(dotted path to a callable)"
            )

        try:
            module_path, _, fn_name = resolver_path.rpartition(".")
            import importlib
            module = importlib.import_module(module_path)
            resolver_fn = getattr(module, fn_name)
            return str(resolver_fn(ref_config))
        except Exception as exc:
            raise SecretResolutionError(f"custom resolver failed: {exc}") from exc


# ── Provider Registry ────────────────────────────────────────────────────────

_PROVIDER_REGISTRY: dict[str, SecretProvider] = {}


def register_provider(name: str, provider: SecretProvider) -> None:
    """Register a secret provider adapter."""
    _PROVIDER_REGISTRY[name] = provider


def get_provider(name: str) -> SecretProvider | None:
    """Look up a registered provider by name."""
    return _PROVIDER_REGISTRY.get(name)


def _register_defaults() -> None:
    """Register all built-in providers."""
    register_provider("env", EnvProvider())
    register_provider("inline", InlineProvider())
    register_provider("vault", VaultProvider())
    register_provider("aws-sm", AwsSmProvider())
    register_provider("gcp-sm", GcpSmProvider())
    register_provider("azure-kv", AzureKvProvider())
    register_provider("custom", CustomProvider())


# Register defaults on import.
_register_defaults()


def resolve(provider: str, ref_config: dict[str, Any]) -> str:
    """Resolve a secret reference to its plaintext value.

    Args:
        provider: The provider name (e.g., ``"vault"``, ``"env"``).
        ref_config: Provider-specific configuration.

    Returns:
        The resolved secret value.

    Raises:
        SecretResolutionError: If the provider is unknown or resolution fails.
    """
    adapter = get_provider(provider)
    if adapter is None:
        raise SecretResolutionError(
            f"no adapter registered for provider {provider!r}"
        )
    return adapter.resolve(ref_config)
