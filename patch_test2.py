import re

with open("scripts/xio_signin_flow.py") as f:
    code = f.read()

code = re.sub(r'GOOGLE_PASSWORD\s*=\s*"[^"]+"', 'GOOGLE_PASSWORD  = "TheTruth1!"', code)

with open("scripts/xio_signin_flow.py", "w") as f:
    f.write(code)
