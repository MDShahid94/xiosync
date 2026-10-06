import re

with open("scripts/xio_signin_flow.py", "r") as f:
    code = f.read()

replacement = """    hdrs: dict[str, str] = {"Content-Type": "application/json"}
    if token:
        hdrs["Authorization"] = f"Bearer {token}"
    if url.startswith(WORKER_AGENT):
        hdrs["X-Worker-Secret"] = "xiogrid-worker-org-secret-2026-karma" """

code = re.sub(
    r'    hdrs: dict\[str, str\] = \{"Content-Type": "application/json"\}\n    if token:\n        hdrs\["Authorization"\] = f"Bearer \{token\}"',
    replacement,
    code,
    flags=re.DOTALL
)

with open("scripts/xio_signin_flow.py", "w") as f:
    f.write(code)
