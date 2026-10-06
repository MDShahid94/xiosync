import re

with open("colab/xiorun_agent.py", "r") as f:
    code = f.read()

replacement = """            # Normal flow: fill email
            cdp_click_element(['input[name="identifier"]', 'input[type="email"]', "#identifierId"])
            cdp_type_text(email)
            time.sleep(rnd(0.5, 1.0))
            cdp_click_element(['#identifierNext', 'button[jsname="LgbsSe"]', 'div[id="identifierNext"]'])"""

code = re.sub(
    r"            # Normal flow: fill email\n            ef = find\(\['input\[name=\"identifier\"\]', 'input\[type=\"email\"\]', \"#identifierId\"\]\)\n            type_human\(ef, email\)\n            time\.sleep\(rnd\(0\.5, 1\.0\)\)\n            find\(\['#identifierNext', 'button\[jsname=\"LgbsSe\"\]', 'div\[id=\"identifierNext\"\]'\]\)\.click\(\)",
    replacement,
    code,
    flags=re.DOTALL
)

with open("colab/xiorun_agent.py", "w") as f:
    f.write(code)
