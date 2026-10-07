import re

with open("colab/xiorun_agent.py") as f:
    code = f.read()

replacement_stay = """        # "Stay signed in?" prompt — CDP native click
        try:
            cdp_click_element(['#confirm-button', '[data-action="confirm"]', 'button[jsname="LgbsSe"]'], timeout=2)
            logger.info("uc-login: 'Stay signed in' confirmed natively")
            time.sleep(1)
        except Exception:
            pass"""

code = re.sub(
    r"        # \"Stay signed in\?\" prompt — JS multi-event \(XIOBR pattern\)\n        try:\n            _stay_clicked = cdp_eval\(\"\"\"\n\(function\(\)\{\n  var dispatch=function\(el\)\{el\.scrollIntoView\(\{block:'center'\}\);\n    \['pointerdown','mousedown','pointerup','mouseup','click'\]\.forEach\(function\(ev\)\{\n      el\.dispatchEvent\(new MouseEvent\(ev,\{bubbles:true,cancelable:true,view:window\}\)\);\}\);\};\n  var b=document\.querySelector\('#confirm-button,\[data-action=\"confirm\"\],button\[jsname=\"LgbsSe\"\]'\);\n  if\(b\)\{dispatch\(b\);return true;\} return false;\n\}\)\(\)\"\"\"\)\n            if _stay_clicked is True:\n                logger\.info\(\"uc-login: 'Stay signed in' confirmed\"\)\n                time\.sleep\(1\)\n        except Exception:\n            pass",
    replacement_stay,
    code,
    flags=re.DOTALL,
)

replacement_skip = """                if any(x in _ps for x in _prompt_keywords):
                    logger.info(f"uc-login: post-login prompt detected (iter {_pl_i+1}) — clicking Skip/Cancel")
                    try:
                        _skip_rect_js = \"\"\"
                        (function(){
                          var all=document.querySelectorAll('button');
                          for(var i=0;i<all.length;i++){
                            var t=(all[i].innerText||all[i].textContent||'').toLowerCase().trim();
                            if(t.includes('cancel')||t.includes('not now')||t.includes('skip')||t.includes('no thanks')){
                              var r = all[i].getBoundingClientRect();
                              return {x:r.x, y:r.y, w:r.width, h:r.height};
                            }
                          } return null;
                        })()
                        \"\"\"
                        rect = cdp_eval(_skip_rect_js)
                        if rect and rect.get('w', 0) > 0:
                            target_x = rect['x'] + rect['w'] * random.uniform(0.3, 0.7)
                            target_y = rect['y'] + rect['h'] * random.uniform(0.3, 0.7)
                            cdp_mouse_move(_mouse_pos["x"], _mouse_pos["y"], target_x, target_y)
                            _mouse_pos["x"], _mouse_pos["y"] = target_x, target_y
                            time.sleep(random.uniform(0.05, 0.15))
                            driver.execute_cdp_cmd("Input.dispatchMouseEvent", {"type": "mousePressed", "button": "left", "clickCount": 1, "x": int(target_x), "y": int(target_y)})
                            time.sleep(random.uniform(0.04, 0.12))
                            driver.execute_cdp_cmd("Input.dispatchMouseEvent", {"type": "mouseReleased", "button": "left", "clickCount": 1, "x": int(target_x), "y": int(target_y)})
                            logger.info("uc-login: post-login prompt dismissed (Skip/Cancel) natively")
                            uc_sleep(2.0, 3.0)
                        else:
                            break
                    except Exception as _ske:
                        logger.warning(f"uc-login: post-login Skip/Cancel failed: {_ske}")
                        break
                else:
                    break"""

code = re.sub(
    r"                if any\(x in _ps for x in _prompt_keywords\):\n                    logger\.info\(f\"uc-login: post-login prompt detected \(iter \{_pl_i\+1\}\) — clicking Skip/Cancel\"\)\n                    try:\n                        from selenium\.webdriver\.support\.ui import WebDriverWait as _WDW\n                        from selenium\.webdriver\.support import expected_conditions as _EC\n                        _skip = _WDW\(driver, 4\)\.until\(_EC\.element_to_be_clickable\(\(\n                            By\.XPATH,\n                            \"//button\[contains\(translate\(\.,'ABCDEFGHIJKLMNOPQRSTUVWXYZ','abcdefghijklmnopqrstuvwxyz'\),'cancel'\)\"\n                            \" or contains\(translate\(\.,'ABCDEFGHIJKLMNOPQRSTUVWXYZ','abcdefghijklmnopqrstuvwxyz'\),'not now'\)\"\n                            \" or contains\(translate\(\.,'ABCDEFGHIJKLMNOPQRSTUVWXYZ','abcdefghijklmnopqrstuvwxyz'\),'skip'\)\"\n                            \" or contains\(translate\(\.,'ABCDEFGHIJKLMNOPQRSTUVWXYZ','abcdefghijklmnopqrstuvwxyz'\),'no thanks'\)\]\"\n                        \)\)\)\n                        _skip\.send_keys\(_PostKeys\.RETURN\)\n                        logger\.info\(\"uc-login: post-login prompt dismissed \(Skip/Cancel\)\"\)\n                        uc_sleep\(2\.0, 3\.0\)\n                    except Exception as _ske:\n                        logger\.warning\(f\"uc-login: post-login Skip/Cancel failed: \{_ske\}\"\)\n                        break\n                else:\n                    break",
    replacement_skip,
    code,
    flags=re.DOTALL,
)

with open("colab/xiorun_agent.py", "w") as f:
    f.write(code)
