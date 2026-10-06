import re

with open("scripts/xio_signin_flow.py", "r") as f:
    code = f.read()

code = re.sub(r'WORKER_TS_IP\s*=\s*"[^"]+"', 'WORKER_TS_IP     = "100.111.130.118"', code)
code = re.sub(r'GOOGLE_ACCOUNT\s*=\s*"[^"]+"', 'GOOGLE_ACCOUNT   = "karmareturnsfromallsides@gmail.com"', code)
code = re.sub(r'GOOGLE_PASS\s*=\s*"[^"]+"', 'GOOGLE_PASS      = "TheTruth1!"', code)
code = re.sub(r'GOOGLE_TOTP\s*=\s*"[^"]+"', 'GOOGLE_TOTP      = "7mx4fx23qiloeurye4pyiknv32spz277"', code)

with open("scripts/xio_signin_flow.py", "w") as f:
    f.write(code)
