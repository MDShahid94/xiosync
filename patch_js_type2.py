import re

with open("colab/xiorun_agent.py", "r") as f:
    code = f.read()

replacement_func = """    def cdp_type_text(text):
        # Bulletproof text insertion for React forms
        js_inject = f'''
        (function(text) {{
            let el = document.activeElement;
            // If body is active, try to find the input we just clicked based on ID/Name
            if (!el || el === document.body) {{
                let inputs = Array.from(document.querySelectorAll('input:not([type="hidden"])'));
                el = inputs.find(i => {{
                    let r = i.getBoundingClientRect();
                    return r.width > 0 && r.height > 0;
                }});
            }}
            if (!el) return false;
            
            // React 16+ value setter bypass
            let nativeInputValueSetter = Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, "value").set;
            if (nativeInputValueSetter) {{
                nativeInputValueSetter.call(el, text);
            }} else {{
                el.value = text;
            }}
            
            el.dispatchEvent(new Event("input", {{ bubbles: true }}));
            el.dispatchEvent(new Event("change", {{ bubbles: true }}));
            return true;
        }})({repr(text)});
        '''
        driver.execute_script(js_inject)
        time.sleep(random.uniform(0.1, 0.3))"""

code = re.sub(
    r'    def cdp_type_text\(text\):\n.*?(?=    def cdp_clear_input)',
    replacement_func + "\n\n",
    code,
    flags=re.DOTALL
)

with open("colab/xiorun_agent.py", "w") as f:
    f.write(code)
