import re

with open("colab/xiorun_agent.py", "r") as f:
    code = f.read()

replacement_func = """    def cdp_clear_input():
        js_inject = '''
        (function() {
            let el = document.activeElement;
            if (!el || el === document.body) {
                let inputs = Array.from(document.querySelectorAll('input:not([type="hidden"])'));
                el = inputs.find(i => {
                    let r = i.getBoundingClientRect();
                    return r.width > 0 && r.height > 0;
                });
            }
            if (!el) return false;
            let nativeInputValueSetter = Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, "value").set;
            if (nativeInputValueSetter) {
                nativeInputValueSetter.call(el, "");
            } else {
                el.value = "";
            }
            el.dispatchEvent(new Event("input", { bubbles: true }));
            el.dispatchEvent(new Event("change", { bubbles: true }));
            return true;
        })();
        '''
        driver.execute_script(js_inject)
        time.sleep(0.1)"""

code = re.sub(
    r'    def cdp_clear_input\(\):\n.*?(?=    _mouse_pos = )',
    replacement_func + "\n\n",
    code,
    flags=re.DOTALL
)

with open("colab/xiorun_agent.py", "w") as f:
    f.write(code)
