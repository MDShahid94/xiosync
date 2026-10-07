with open("scripts/xio_signin_flow.py") as f:
    content = f.read()

content = content.replace(
    'print(f"  Profile  : {DRIVE_OBJECT_KEY}  (saved to Drive ✅)")',
    'print(f"  Profile  : {DRIVE_OBJECT_KEY}  " + ("(saved to Drive ✅)" if login_resp.get("ok") else "(NOT saved ❌)"))',
)

with open("scripts/xio_signin_flow.py", "w") as f:
    f.write(content)
