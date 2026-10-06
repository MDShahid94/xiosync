import re

with open("colab/xiorun_agent.py", "r") as f:
    code = f.read()

code = re.sub(r'^\s*opts\.add_experimental_option\("useAutomationExtension", False\)\n', '', code, flags=re.MULTILINE)

with open("colab/xiorun_agent.py", "w") as f:
    f.write(code)
