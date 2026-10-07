import re

with open("colab/xiorun_agent.py") as f:
    code = f.read()

code = re.sub(
    r'^\s*opts\.add_experimental_option\("excludeSwitches", \["enable-automation"\]\)\n',
    "",
    code,
    flags=re.MULTILINE,
)

with open("colab/xiorun_agent.py", "w") as f:
    f.write(code)
