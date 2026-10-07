import re

with open("scripts/xio_signin_flow.py") as f:
    code = f.read()

# I need to find where local_dir is constructed in xio_signin_flow.py
# In Step 6, it uses `local_dir = profile or f"/tmp/uc-profile-{SESSION_ID}"`
# I should change it to use the new determinisitic path
new_step6 = """    # ── Step 6: Push profile to Drive ───────────────────────────────────────
    MIN_COOKIES = 30
    banner("Step 6 · Pushing Chrome profile to Google Drive")
    
    # Reconstruct the deterministic path exactly as the worker calculates it
    node_slug = "xiogrid__default__worker_006"
    local_dir = f"/tmp/xiorun_profiles/PRFL_{ACCOUNT_KEY}__{node_slug}"
    
    print(f"   Drive key : {DRIVE_OBJECT_KEY}")
    print(f"   Local dir : {local_dir}")"""

code = re.sub(
    r'    # ── Step 6: Push profile to Drive ───────────────────────────────────────\n.*?    print\(f"   Local dir : \{local_dir\}"\)',
    new_step6,
    code,
    flags=re.DOTALL,
)

with open("scripts/xio_signin_flow.py", "w") as f:
    f.write(code)
