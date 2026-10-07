import re

with open("scripts/xio_signin_flow.py") as f:
    code = f.read()

replacement = """    # ── Step 2: Acquire PPPoE slot (BYPASSED) ─────────────────
    banner("Step 2 · Acquiring residential PPPoE slot (BYPASSED)")
    ppp_slot   = 1
    public_ip  = "123.123.123.123"
    proxy_url  = "socks5://127.0.0.1:19056"
    print(f"✅ Slot {ppp_slot} assigned (bypassed)")
    print(f"   Worker proxy: {proxy_url}")"""

code = re.sub(
    r'    # ── Step 2: Acquire PPPoE slot \(per-account IP pinning\) ─────────────────\n    banner\("Step 2 · Acquiring residential PPPoE slot"\).*?print\(f"   Worker proxy: \{proxy_url\}"\)',
    replacement,
    code,
    flags=re.DOTALL,
)

# Also bypass step 1 Auth since we don't need the token anymore
replacement_auth = """    # ── Step 1: Auth (BYPASSED) ────────────────────────────────────────────────────────
    banner("Step 1 · Authenticating with XIOSYNC (BYPASSED)")
    token = "fake-token" """

code = re.sub(
    r'    # ── Step 1: Auth ────────────────────────────────────────────────────────\n    banner\("Step 1 · Authenticating with XIOSYNC"\).*?print\(f"✅ Token acquired \(\{len\(token\)\} chars\)"\)',
    replacement_auth,
    code,
    flags=re.DOTALL,
)

with open("scripts/xio_signin_flow.py", "w") as f:
    f.write(code)
