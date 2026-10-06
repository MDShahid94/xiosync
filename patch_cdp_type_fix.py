import re

with open("colab/xiorun_agent.py", "r") as f:
    code = f.read()

replacement_func = """    def cdp_type_text(text):
        for ch in text:
            driver.execute_cdp_cmd("Input.dispatchKeyEvent", {"type": "char", "text": ch})
            time.sleep(random.uniform(0.02, 0.08))"""

code = re.sub(
    r'    def cdp_type_text\(text\):\n        for ch in text:\n            driver\.execute_cdp_cmd\("Input\.dispatchKeyEvent", \{"type": "keyDown", "text": ch\}\)\n            driver\.execute_cdp_cmd\("Input\.dispatchKeyEvent", \{"type": "char", "text": ch\}\)\n            time\.sleep\(random\.uniform\(0\.01, 0\.05\)\)\n            driver\.execute_cdp_cmd\("Input\.dispatchKeyEvent", \{"type": "keyUp", "text": ch\}\)\n            time\.sleep\(random\.uniform\(0\.02, 0\.08\)\)',
    replacement_func,
    code
)

with open("colab/xiorun_agent.py", "w") as f:
    f.write(code)
