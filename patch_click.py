import re

with open("colab/xiorun_agent.py", "r") as f:
    code = f.read()

replacement_func = """    def cdp_click_element(selectors, max_wait=5):
        # Extremely robust CDP clicker that falls back to JS DOM clicking if native fails
        for _ci in range(int(max_wait * 2)):
            time.sleep(0.5)
            for sel in selectors:
                try:
                    js = f'''
                    (function() {{
                      var el = document.querySelector("{sel}");
                      if(!el) return null;
                      var r = el.getBoundingClientRect();
                      if(r.width>0 && r.height>0) {{
                          // Fallback JS focus just in case CDP fails
                          el.focus();
                          return {{x: r.x + r.width*0.5, y: r.y + r.height*0.5, w: r.width, h: r.height}};
                      }}
                      return null;
                    }})()'''
                    rect = cdp_eval(js)
                    if rect and rect.get('w', 0) > 0:
                        x, y = int(rect['x']), int(rect['y'])
                        cdp_mouse_move(_mouse_pos["x"], _mouse_pos["y"], x, y)
                        _mouse_pos["x"], _mouse_pos["y"] = x, y
                        time.sleep(0.1)
                        driver.execute_cdp_cmd("Input.dispatchMouseEvent", {"type": "mousePressed", "button": "left", "clickCount": 1, "x": x, "y": y})
                        time.sleep(0.1)
                        driver.execute_cdp_cmd("Input.dispatchMouseEvent", {"type": "mouseReleased", "button": "left", "clickCount": 1, "x": x, "y": y})
                        
                        # Extra JS click as a fallback in case the CDP event was swallowed
                        js_click = f"document.querySelector('{sel}').click();"
                        try: driver.execute_script(js_click)
                        except: pass
                        
                        return True
                except Exception:
                    pass
        raise RuntimeError(f"Element not found for CDP click: {selectors}")"""

code = re.sub(
    r'    def cdp_click_element\(selectors, max_wait=5\):\n.*?(?=    def cdp_type_text)',
    replacement_func + "\n\n",
    code,
    flags=re.DOTALL
)

with open("colab/xiorun_agent.py", "w") as f:
    f.write(code)
