with open("colab/xiorun_agent.py") as f:
    code = f.read()

code = code.replace(
    "    exit_node_public_ip: str | None = None   # PPPoE slot public IP for timezone geo-resolve\n    identity_id:         str | None = None   # if set, login-start uses pre-pulled PRFL profile",
    "    exit_node_public_ip: str | None = None   # PPPoE slot public IP for timezone geo-resolve\n    identity_id:         str | None = None   # if set, login-start uses pre-pulled PRFL profile\n    fingerprint:         dict = {}           # Pass full FingerprintSpec",
)

# Add fingerprint to _run_uc_login_sync arguments
code = code.replace(
    "    exit_node_public_ip: str | None = None,  # Expected residential IP for preflight IP match\n) -> dict:",
    "    exit_node_public_ip: str | None = None,  # Expected residential IP for preflight IP match\n    fingerprint: dict | None = None,         # Full hardware spec\n) -> dict:",
)

# Pass it from run_uc_login endpoint
code = code.replace(
    "        exit_node_public_ip=req.exit_node_public_ip,\n    )",
    "        exit_node_public_ip=req.exit_node_public_ip,\n        fingerprint=req.fingerprint,\n    )",
)

with open("colab/xiorun_agent.py", "w") as f:
    f.write(code)
