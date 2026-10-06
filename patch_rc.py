import re

with open("colab/xiorun_agent.py", "r") as f:
    code = f.read()

replacement = """                                    cdp_click_element("input[type='password']")
                                    uc_sleep(0.3, 0.5)
                                    cdp_type_text(password)
                                    uc_sleep(0.4, 0.6)
                                    cdp_click_element('#passwordNext, button[type=submit]')
                                    uc_sleep(3.0, 5.0)"""

code = re.sub(
    r"                                    _rc_pw\.click\(\)\n                                    uc_sleep\(0\.3, 0\.5\)\n                                    driver\.execute_script\(\"var el=arguments\[0\]; el\.focus\(\); if\(el\._valueTracker\)\{el\._valueTracker\.setValue\(''\);\}\", _rc_pw\)\n                                    driver\.execute_cdp_cmd\(\"Input\.insertText\", \{\"text\": password\}\)\n                                    uc_sleep\(0\.4, 0\.6\)\n                                    cdp_eval\(\"\(function\(\)\{var b=document\.querySelector\('#passwordNext,button\[type=submit\]'\);if\(b\)\{b\.click\(\);return true;\}return false;\}\)\(\)\"\)\n                                    uc_sleep\(3\.0, 5\.0\)",
    replacement,
    code,
    flags=re.DOTALL
)

with open("colab/xiorun_agent.py", "w") as f:
    f.write(code)
