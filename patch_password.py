import re

with open("colab/xiorun_agent.py") as f:
    code = f.read()

replacement = """            # ── Handle "Choose how you want to sign in" (passkey selection page) ──
            for _cpw_i in range(8):   # poll up to 4s
                time.sleep(0.5)
                _bt2 = cdp_eval("document.body.innerText||''") or ""
                if "enter your password" in _bt2.lower() or "enter password" in _bt2.lower():
                    # Find exact element using JS bounding client rect
                    _choose_pw_rect_js = \"\"\"
                    (function(){
                      var all=document.querySelectorAll('*');
                      for(var i=0;i<all.length;i++){
                        var t=(all[i].childElementCount===0?(all[i].innerText||all[i].textContent||''):'').toLowerCase().trim();
                        if(t.includes('enter your password')||t.includes('enter password')){
                          var r = all[i].getBoundingClientRect();
                          return {x:r.x, y:r.y, w:r.width, h:r.height};
                        }
                      } return null;
                    })()
                    \"\"\"
                    rect = cdp_eval(_choose_pw_rect_js)
                    if rect and rect.get('w', 0) > 0:
                        target_x = rect['x'] + rect['w'] * random.uniform(0.3, 0.7)
                        target_y = rect['y'] + rect['h'] * random.uniform(0.3, 0.7)
                        cdp_mouse_move(_mouse_pos["x"], _mouse_pos["y"], target_x, target_y)
                        _mouse_pos["x"], _mouse_pos["y"] = target_x, target_y
                        time.sleep(random.uniform(0.05, 0.15))
                        driver.execute_cdp_cmd("Input.dispatchMouseEvent", {"type": "mousePressed", "button": "left", "clickCount": 1, "x": int(target_x), "y": int(target_y)})
                        time.sleep(random.uniform(0.04, 0.12))
                        driver.execute_cdp_cmd("Input.dispatchMouseEvent", {"type": "mouseReleased", "button": "left", "clickCount": 1, "x": int(target_x), "y": int(target_y)})
                        logger.info(f"uc-login: 'Enter your password' clicked via CDP (passkey screen) — attempt {_cpw_i+1}")
                        time.sleep(1.5)   # wait for password input to render
                        break
                elif "password" in _bt2.lower() and "enter" not in _bt2.lower():
                    # Already on password input page
                    break

        # ── Password — CDP Input (isTrusted=true, native interaction) ────────
        curr_pw = driver.current_url
        logger.info(f"uc-login: filling password on {curr_pw[:80]}")
        
        try:
            cdp_click_element(['input[type="password"]', 'input[name="Passwd"]', 'input[name="password"]'], 15)
            uc_sleep(0.4, 0.8)
            driver.execute_cdp_cmd("Input.insertText", {"text": password})
            logger.info("uc-login: password typed via Input.insertText (isTrusted)")
        except Exception as pw_err:
            logger.warning(f"uc-login: password CDP failed ({pw_err}) — fallback to cdp_type_text")
            try:
                cdp_click_element(['input[type="password"]', 'input[name="Passwd"]', 'input[name="password"]'], 8)
                uc_sleep(0.2, 0.4)
                cdp_type_text(password)
            except Exception as pw2:
                logger.warning(f"uc-login: cdp_type_text also failed: {pw2}")

        uc_sleep(0.5, 1.0)
        try:
            cdp_click_element(['#passwordNext', 'button[jsname="LgbsSe"]', 'div[id="passwordNext"]', 'button[type="submit"]'], 5)
        except Exception as _btn_err:
            logger.warning(f"uc-login: password next button fallback ({_btn_err})")
        logger.info("uc-login: password submitted")"""

code = re.sub(
    r"            # ── Handle \"Choose how you want to sign in\" \(passkey selection page\) ──(.*?)logger\.info\(\"uc-login: password submitted\"\)",
    replacement,
    code,
    flags=re.DOTALL,
)

with open("colab/xiorun_agent.py", "w") as f:
    f.write(code)
