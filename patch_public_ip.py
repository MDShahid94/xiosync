with open("scripts/xio_signin_flow.py") as f:
    code = f.read()

replacement = """    try:
        import subprocess
        _ip = subprocess.check_output(
            ["curl", "-s", "--max-time", "10", "-x", "socks5h://100.106.81.15:10001", "https://api.ipify.org"], 
            text=True
        ).strip()
        public_ip = _ip if _ip else "100.106.81.15"
    except Exception:
        public_ip = "100.106.81.15"
"""

code = code.replace('    public_ip  = "100.106.81.15"', replacement)

with open("scripts/xio_signin_flow.py", "w") as f:
    f.write(code)
