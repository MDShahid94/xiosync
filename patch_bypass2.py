import re

with open("scripts/xio_signin_flow.py") as f:
    code = f.read()

code = re.sub(
    r'proxy_url\s*=\s*"socks5://127\.0\.0\.1:19056"',
    'proxy_url  = "socks5://127.0.0.1:19055"',
    code,
)

with open("scripts/xio_signin_flow.py", "w") as f:
    f.write(code)
