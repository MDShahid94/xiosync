import re

with open("colab/xiorun_agent.py", "r") as f:
    code = f.read()

replacement_func = """    def cdp_type_text(text):
        # Bulletproof text insertion for React forms
        js_inject = f'''
        (function(text) {{
            let el = document.activeElement;
            if(!el) return false;
            let lastValue = el.value;
            el.value = text;
            let event = new Event("input", {{ bubbles: true }});
            // React 15/16/17 hack
            let tracker = el._valueTracker;
            if (tracker) {{
                tracker.setValue(lastValue);
            }}
            // Trigger React onChange
            let ev = new Event("input", {{ bubbles: true }});
            el.dispatchEvent(ev);
            let change = new Event("change", {{ bubbles: true }});
            el.dispatchEvent(change);
            return true;
        }})({repr(text)});
        '''
        driver.execute_script(js_inject)
        time.sleep(random.uniform(0.1, 0.3))"""

code = re.sub(
    r'    def cdp_type_text\(text\):\n        for ch in text:\n            driver\.execute_cdp_cmd.*?(?=    def |$)',
    replacement_func + "\n\n",
    code,
    flags=re.DOTALL
)

with open("colab/xiorun_agent.py", "w") as f:
    f.write(code)
