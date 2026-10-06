import re

with open("scripts/xio_signin_flow.py", "r") as f:
    code = f.read()

replacement_func = """ACCOUNT_KEY      = GOOGLE_ACCOUNT.split("@")[0].replace(".", "_")
# Drive profile key (Matching XIOSYNC standard: chrome_profiles/PRFL-XXX_slug.tar.gz)
DRIVE_OBJECT_KEY = f"chrome_profiles/PRFL-999_{ACCOUNT_KEY}.tar.gz" """

code = code.replace(
    '# Drive profile key (Matching XIOSYNC standard: chrome_profiles/PRFL-XXX_slug.tar.gz)\nDRIVE_OBJECT_KEY = f"chrome_profiles/PRFL-999_{ACCOUNT_KEY}.tar.gz"',
    replacement_func
)

with open("scripts/xio_signin_flow.py", "w") as f:
    f.write(code)
