with open("colab/xiorun_agent.py", "r") as f:
    code = f.read()

# Original:
# cdp_click_element(['input[type="password"]', 'input[name="Passwd"]', 'input[name="password"]'], 15)
# uc_sleep(0.5, 1.0)
# cdp_type_text(password)

# Replacing with longer wait
code = code.replace(
    "cdp_click_element(['input[type=\"password\"]', 'input[name=\"Passwd\"]', 'input[name=\"password\"]'], 15)\n            uc_sleep(0.5, 1.0)",
    "cdp_click_element(['input[type=\"password\"]', 'input[name=\"Passwd\"]', 'input[name=\"password\"]'], 15)\n            time.sleep(3.0)\n            uc_sleep(0.5, 1.0)"
)

with open("colab/xiorun_agent.py", "w") as f:
    f.write(code)
