import re

with open("xiosync/subsystems/xiorun/node_client.py") as f:
    code = f.read()

# 1. Add secret initialization
replacement_init = """    def __init__(self, tailscale_ip: str, port: int = AGENT_PORT, secret: str = "") -> None:
        self._base = f"http://{tailscale_ip}:{port}"
        self._tailscale_ip = tailscale_ip
        from xiosync.core.config import settings
        self._secret = secret or settings.WORKER_SECRET.get_secret_value() if hasattr(settings.WORKER_SECRET, 'get_secret_value') else str(settings.WORKER_SECRET)
        self._headers = {"X-Worker-Secret": self._secret}"""

code = re.sub(
    r"    def __init__\(self, tailscale_ip: str, port: int = AGENT_PORT\) -> None:\n        self\._base = f\"http://\{tailscale_ip\}:\{port\}\"\n        self\._tailscale_ip = tailscale_ip",
    replacement_init,
    code,
    flags=re.DOTALL,
)

# 2. Add headers to client initialization globally
code = code.replace(
    "async with httpx.AsyncClient(timeout=",
    "async with httpx.AsyncClient(headers=self._headers, timeout=",
)

with open("xiosync/subsystems/xiorun/node_client.py", "w") as f:
    f.write(code)
