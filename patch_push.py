import re

with open("scripts/xio_signin_flow.py") as f:
    code = f.read()

code = re.sub(
    r'DRIVE_OBJECT_KEY = f"chrome_profiles/PRFL-999_\{ACCOUNT_KEY\}\.tar\.gz"',
    'DRIVE_OBJECT_KEY = "profiles/PRFL-001.tar.gz"',
    code,
)

with open("scripts/xio_signin_flow.py", "w") as f:
    f.write(code)
