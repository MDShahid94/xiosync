import re

with open("colab/xiorun_agent.py", "r") as f:
    code = f.read()

replacement_func = """    def cdp_type_text(text):
        driver.execute_cdp_cmd("Input.insertText", {"text": text})
        time.sleep(0.1)
        # Dispatch a space and backspace to trigger React's synthetic onChange events cleanly
        driver.execute_cdp_cmd("Input.dispatchKeyEvent", {"type": "keyDown", "text": " ", "unmodifiedText": " "})
        driver.execute_cdp_cmd("Input.dispatchKeyEvent", {"type": "char", "text": " ", "unmodifiedText": " "})
        driver.execute_cdp_cmd("Input.dispatchKeyEvent", {"type": "keyUp", "text": " ", "unmodifiedText": " "})
        time.sleep(0.1)
        driver.execute_cdp_cmd("Input.dispatchKeyEvent", {"type": "keyDown", "windowsVirtualKeyCode": 8, "key": "Backspace"})
        driver.execute_cdp_cmd("Input.dispatchKeyEvent", {"type": "keyUp", "windowsVirtualKeyCode": 8, "key": "Backspace"})
        time.sleep(0.1)"""

code = re.sub(
    r'    def cdp_type_text\(text\):\n        for ch in text:\n            driver\.execute_cdp_cmd\("Input\.dispatchKeyEvent", \{"type": "char", "text": ch\}\)\n            time\.sleep\(random\.uniform\(0\.02, 0\.08\)\)',
    replacement_func,
    code
)

with open("colab/xiorun_agent.py", "w") as f:
    f.write(code)
