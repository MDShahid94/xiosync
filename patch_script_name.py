import re

with open("scripts/xio_signin_flow.py") as f:
    code = f.read()

# Replace the DRIVE_OBJECT_KEY logic
code = re.sub(
    r"# Drive profile key.*?\nDRIVE_OBJECT_KEY = [^\n]*\n",
    '# Drive profile key (Matching XIOSYNC standard: chrome_profiles/PRFL-XXX_slug.tar.gz)\nDRIVE_OBJECT_KEY = f"chrome_profiles/PRFL-999_{ACCOUNT_KEY}.tar.gz"\n',
    code,
    flags=re.DOTALL,
)

with open("scripts/xio_signin_flow.py", "w") as f:
    f.write(code)
