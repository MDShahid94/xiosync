import re

with open("xiosync/subsystems/xiogrid/domain/pppoe.py") as f:
    code = f.read()

code = re.sub(r"return name", 'return "xiogrid-default-linux"', code)
code = re.sub(r'return "Mac M4 Pro"', 'return "xiogrid-default-linux"', code)

with open("xiosync/subsystems/xiogrid/domain/pppoe.py", "w") as f:
    f.write(code)
