import re

with open("scripts/xio_signin_flow.py", "r") as f:
    code = f.read()

code = re.sub(
    r'GOOGLE_TOTP\s*= "7mx4fx23qiloeurye4pyiknv32spz277"',
    'GOOGLE_TOTP      = "eupfnmzj7syeqqkj2w7hpbajamvn236f"',
    code
)

with open("scripts/xio_signin_flow.py", "w") as f:
    f.write(code)
