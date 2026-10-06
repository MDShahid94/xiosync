import re

with open("colab/xiorun_agent.py", "r") as f:
    code = f.read()

# Replace Input.insertText with cdp_type_text for password
replacement = """            cdp_click_element(['input[type="password"]', 'input[name="Passwd"]', 'input[name="password"]'], 15)
            uc_sleep(0.4, 0.8)
            cdp_type_text(password)
            logger.info("uc-login: password typed via cdp_type_text (isTrusted)")"""

code = re.sub(
    r'            cdp_click_element\(\[\'input\[type="password"\]\', \'input\[name="Passwd"\]\', \'input\[name="password"\]\'\], 15\)\n            uc_sleep\(0\.4, 0\.8\)\n            driver\.execute_cdp_cmd\("Input\.insertText", \{"text": password\}\)\n            logger\.info\("uc-login: password typed via Input\.insertText \(isTrusted\)"\)',
    replacement,
    code,
    flags=re.DOTALL
)

# Also fix email if it uses insertText
replacement_email = """            cdp_click_element(['input[name="identifier"]', 'input[type="email"]', '#identifierId'], 15)
            uc_sleep(0.4, 1.2)
            cdp_type_text(email)
            logger.info("uc-login: email entered via cdp_type_text")"""

code = re.sub(
    r'            cdp_click_element\(\[\'input\[name="identifier"\]\', \'input\[type="email"\]\', \'#identifierId\'\], 15\)\n            uc_sleep\(0\.4, 1\.2\)\n            driver\.execute_cdp_cmd\("Input\.insertText", \{"text": email\}\)\n            logger\.info\("uc-login: email entered"\)',
    replacement_email,
    code,
    flags=re.DOTALL
)

with open("colab/xiorun_agent.py", "w") as f:
    f.write(code)
