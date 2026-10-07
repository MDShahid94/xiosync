from __future__ import annotations

import abc
from dataclasses import dataclass, field
from typing import Any


@dataclass
class GenerationResult:
    text: str = ""
    provider: str = ""
    model: str = ""
    success: bool = True
    error: str | None = None
    usage: dict[str, Any] = field(default_factory=dict)


class GenerationProvider(abc.ABC):
    @property
    @abc.abstractmethod
    def name(self) -> str: ...

    @abc.abstractmethod
    async def generate(
        self,
        prompt: str,
        *,
        system: str = "",
        output_format: str = "text",
        json_schema: dict | None = None,
        temperature: float = 0.2,
        max_tokens: int = 8192,
        timeout: int = 120,
    ) -> GenerationResult: ...

    @classmethod
    @abc.abstractmethod
    def is_available(cls) -> bool: ...
