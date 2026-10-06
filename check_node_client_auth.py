import re

with open("xiosync/subsystems/xiorun/node_client.py", "r") as f:
    code = f.read()

print("Contains headers=" in code, "headers" in code)
