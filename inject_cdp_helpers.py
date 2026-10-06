import re

with open("colab/xiorun_agent.py", "r") as f:
    code = f.read()

helpers = """    # ── CDP Native Behavioral Interactions ──────────────────────────────────
    _mouse_pos = {"x": 100, "y": 100}

    def _bezier(p0, p1, p2, p3, t):
        x = (1-t)**3 * p0[0] + 3*(1-t)**2 * t * p1[0] + 3*(1-t) * t**2 * p2[0] + t**3 * p3[0]
        y = (1-t)**3 * p0[1] + 3*(1-t)**2 * t * p1[1] + 3*(1-t) * t**2 * p2[1] + t**3 * p3[1]
        return x, y

    def cdp_mouse_move(start_x, start_y, end_x, end_y):
        steps = random.randint(12, 25)
        cx1 = start_x + (end_x - start_x) * random.uniform(0.2, 0.8) + random.uniform(-30, 30)
        cy1 = start_y + (end_y - start_y) * random.uniform(0.2, 0.8) + random.uniform(-30, 30)
        cx2 = start_x + (end_x - start_x) * random.uniform(0.2, 0.8) + random.uniform(-30, 30)
        cy2 = start_y + (end_y - start_y) * random.uniform(0.2, 0.8) + random.uniform(-30, 30)
        for i in range(steps + 1):
            t = i / steps
            x, y = _bezier((start_x, start_y), (cx1, cy1), (cx2, cy2), (end_x, end_y), t)
            driver.execute_cdp_cmd("Input.dispatchMouseEvent", {"type": "mouseMoved", "x": int(x), "y": int(y)})
            time.sleep(random.uniform(0.005, 0.015))

    def cdp_get_rect(selector):
        # We must use execute_cdp_cmd for evaluate to be fully untracked
        js = f\"\"\"
        (function() {{
            var el = document.querySelector('{selector}');
            if (!el) return null;
            var rect = el.getBoundingClientRect();
            return {{x: rect.x, y: rect.y, w: rect.width, h: rect.height}};
        }})()
        \"\"\"
        try:
            res = driver.execute_cdp_cmd("Runtime.evaluate", {"expression": js, "returnByValue": True})
            return res.get("result", {}).get("value")
        except:
            return None

    def cdp_click_element(selectors, timeout=10):
        if isinstance(selectors, str): selectors = [selectors]
        rect = None
        for _ in range(int(timeout * 2.5)):
            for sel in selectors:
                rect = cdp_get_rect(sel)
                if rect and rect.get('w', 0) > 0 and rect.get('h', 0) > 0:
                    break
            if rect and rect.get('w', 0) > 0 and rect.get('h', 0) > 0:
                break
            time.sleep(0.4)
        if not rect or rect.get('w', 0) <= 0:
            raise Exception(f"Element not found for CDP click: {selectors}")
        
        target_x = rect['x'] + rect['w'] * random.uniform(0.3, 0.7)
        target_y = rect['y'] + rect['h'] * random.uniform(0.3, 0.7)
        
        cdp_mouse_move(_mouse_pos["x"], _mouse_pos["y"], target_x, target_y)
        _mouse_pos["x"], _mouse_pos["y"] = target_x, target_y
        
        time.sleep(random.uniform(0.05, 0.15))
        driver.execute_cdp_cmd("Input.dispatchMouseEvent", {"type": "mousePressed", "button": "left", "clickCount": 1, "x": int(target_x), "y": int(target_y)})
        time.sleep(random.uniform(0.04, 0.12))
        driver.execute_cdp_cmd("Input.dispatchMouseEvent", {"type": "mouseReleased", "button": "left", "clickCount": 1, "x": int(target_x), "y": int(target_y)})
        return True

    def cdp_type_text(text):
        for ch in text:
            driver.execute_cdp_cmd("Input.dispatchKeyEvent", {"type": "keyDown", "text": ch})
            time.sleep(random.uniform(0.03, 0.12))
            driver.execute_cdp_cmd("Input.dispatchKeyEvent", {"type": "keyUp", "text": ch})
            time.sleep(random.uniform(0.02, 0.08))

    def cdp_clear_input():
        driver.execute_cdp_cmd("Input.dispatchKeyEvent", {"type": "keyDown", "windowsVirtualKeyCode": 17, "modifiers": 2}) # Ctrl
        driver.execute_cdp_cmd("Input.dispatchKeyEvent", {"type": "keyDown", "windowsVirtualKeyCode": 65, "modifiers": 2}) # A
        driver.execute_cdp_cmd("Input.dispatchKeyEvent", {"type": "keyUp", "windowsVirtualKeyCode": 65, "modifiers": 2})
        driver.execute_cdp_cmd("Input.dispatchKeyEvent", {"type": "keyUp", "windowsVirtualKeyCode": 17, "modifiers": 0})
        time.sleep(0.05)
        driver.execute_cdp_cmd("Input.dispatchKeyEvent", {"type": "keyDown", "windowsVirtualKeyCode": 8, "key": "Backspace"})
        driver.execute_cdp_cmd("Input.dispatchKeyEvent", {"type": "keyUp", "windowsVirtualKeyCode": 8, "key": "Backspace"})
        time.sleep(0.05)"""

code = re.sub(
    r"    def type_human\(el, text\):(.*?)(?=    try:\n        # ── CDP helper)",
    helpers + "\n\n",
    code,
    flags=re.DOTALL
)

with open("colab/xiorun_agent.py", "w") as f:
    f.write(code)
