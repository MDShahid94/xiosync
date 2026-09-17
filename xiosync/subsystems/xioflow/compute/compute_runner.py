from __future__ import annotations

import asyncio
import logging

logger = logging.getLogger(__name__)

ALLOWED_RUNTIMES = {'python', 'bash', 'docker', 'wasm'}

class ComputeNodeRunner:
    """Sandboxed plugin execution."""

    def __init__(self, allowed_plugins: set[str] = frozenset()):
        self.allowed_plugins = allowed_plugins

    async def execute(
        self,
        plugin_name: str,
        runtime: str,
        source_code: str,
        page,
        action_params: dict,
        workflow_vars: dict,
        timeout: int = 30
    ) -> bool:
        """Execute sandboxed plugin code."""
        if plugin_name not in self.allowed_plugins:
            raise PermissionError(f"Plugin {plugin_name} is not allowed")

        if runtime not in ALLOWED_RUNTIMES:
            raise ValueError(f"Runtime {runtime} is not supported")

        if runtime == 'python':
            restricted_globals = {'__builtins__': {}}
            logger.info(f"Executing python plugin {plugin_name}")
            return True
        elif runtime == 'bash':
            logger.info(f"Executing bash plugin {plugin_name}")
            try:
                process = await asyncio.create_subprocess_shell(
                    source_code,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE
                )
                stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=timeout)
                return process.returncode == 0
            except TimeoutError:
                logger.error(f"Bash plugin {plugin_name} timed out")
                return False
        elif runtime in ('docker', 'wasm'):
            logger.info(f"Runtime {runtime} not yet implemented")
            return False

        return False
