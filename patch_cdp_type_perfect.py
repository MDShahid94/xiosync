import re

with open("colab/xiorun_agent.py", "r") as f:
    code = f.read()

replacement_func = """    def cdp_type_text(text):
        for ch in text:
            driver.execute_cdp_cmd("Input.dispatchKeyEvent", {"type": "keyDown"})
            driver.execute_cdp_cmd("Input.dispatchKeyEvent", {"type": "char", "text": ch, "unmodifiedText": ch})
            time.sleep(random.uniform(0.01, 0.05))
            driver.execute_cdp_cmd("Input.dispatchKeyEvent", {"type": "keyUp"})
            time.sleep(random.uniform(0.02, 0.08))"""

# regex to replace current cdp_type_text
code = re.sub(
    r'    def cdp_type_text\(text\):\n        driver\.execute_cdp_cmd.*?(?=    def |$)',
    replacement_func + "\n\n",
    code,
    flags=re.DOTALL
)

with open("colab/xiorun_agent.py", "w") as f:
    f.write(code)
