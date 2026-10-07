import re

with open("scripts/xio_signin_flow.py") as f:
    code = f.read()

code = re.sub(
    r'GOOGLE_ACCOUNT\s*= "karmareturnsfromallsides@gmail\.com"',
    'GOOGLE_ACCOUNT     = "shahid.raiganj@gmail.com"',
    code,
)

code = re.sub(
    r'TOTP_SECRET\s*= "7mx4fx23qiloeurye4pyiknv32spz277"',
    'TOTP_SECRET        = "eupfnmzj7syeqqkj2w7hpbajamvn236f"',
    code,
)

code = re.sub(
    r'DRIVE_OBJECT_KEY\s*= "profiles/PRFL-001\.tar\.gz"',
    'DRIVE_OBJECT_KEY = "profiles/PRFL-005.tar.gz"',
    code,
)

with open("scripts/xio_signin_flow.py", "w") as f:
    f.write(code)
