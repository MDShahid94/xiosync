import re

with open("colab/xiorun_agent.py", "r") as f:
    code = f.read()

replacement_click = """        if not rect or rect.get('w', 0) <= 0:
            raise Exception(f"Element not found for CDP click: {selectors}")
            
        # ── Pre-interaction Scrolling Seasoning ──
        if random.random() < 0.4:
            scroll_dir = random.choice([100, 200, -100])
            driver.execute_cdp_cmd("Input.dispatchMouseEvent", {
                "type": "mouseWheel", "x": _mouse_pos["x"], "y": _mouse_pos["y"],
                "deltaX": 0, "deltaY": scroll_dir
            })
            time.sleep(random.uniform(0.1, 0.3))
            driver.execute_cdp_cmd("Input.dispatchMouseEvent", {
                "type": "mouseWheel", "x": _mouse_pos["x"], "y": _mouse_pos["y"],
                "deltaX": 0, "deltaY": -scroll_dir
            })
            time.sleep(random.uniform(0.1, 0.3))
        
        target_x = rect['x'] + rect['w'] * random.uniform(0.3, 0.7)"""

code = re.sub(
    r"        if not rect or rect\.get\('w', 0\) <= 0:\n            raise Exception\(f\"Element not found for CDP click: \{selectors\}\"\)\n        \n        target_x = rect\['x'\] \+ rect\['w'\] \* random\.uniform\(0\.3, 0\.7\)",
    replacement_click,
    code,
    flags=re.DOTALL
)

replacement_sleep = """        def uc_sleep(a=0.8, b=2.0):
            duration = a + random.random() * (b - a)
            end_time = time.time() + duration
            while time.time() < end_time:
                # ── Idle Jitter Seasoning ──
                if random.random() < 0.35:
                    tgt_x = max(10, min(1900, _mouse_pos["x"] + random.randint(-80, 80)))
                    tgt_y = max(10, min(1000, _mouse_pos["y"] + random.randint(-80, 80)))
                    try:
                        cdp_mouse_move(_mouse_pos["x"], _mouse_pos["y"], tgt_x, tgt_y)
                        _mouse_pos["x"], _mouse_pos["y"] = tgt_x, tgt_y
                    except:
                        pass
                rem = end_time - time.time()
                if rem <= 0: break
                time.sleep(min(rem, random.uniform(0.1, 0.4)))"""

code = re.sub(
    r"        def uc_sleep\(a=0\.8, b=2\.0\):\n            time\.sleep\(a \+ random\.random\(\) \* \(b - a\)\)",
    replacement_sleep,
    code,
    flags=re.DOTALL
)

with open("colab/xiorun_agent.py", "w") as f:
    f.write(code)
