import re

with open("colab/xiorun_agent.py", "r") as f:
    code = f.read()

replacement_taw = """                        if _taw_btn and _taw_btn.is_displayed():
                            _choose_taw_rect_js = \"\"\"
                            (function(){
                              var all=document.querySelectorAll('*');
                              for(var i=0;i<all.length;i++){
                                var t=(all[i].childElementCount===0?(all[i].innerText||all[i].textContent||''):'').toLowerCase().trim();
                                if(t.includes('try another')||t.includes('more options')){
                                  var r = all[i].getBoundingClientRect();
                                  return {x:r.x, y:r.y, w:r.width, h:r.height};
                                }
                              } return null;
                            })()
                            \"\"\"
                            rect = cdp_eval(_choose_taw_rect_js)
                            if rect and rect.get('w', 0) > 0:
                                target_x = rect['x'] + rect['w'] * random.uniform(0.3, 0.7)
                                target_y = rect['y'] + rect['h'] * random.uniform(0.3, 0.7)
                                cdp_mouse_move(_mouse_pos["x"], _mouse_pos["y"], target_x, target_y)
                                _mouse_pos["x"], _mouse_pos["y"] = target_x, target_y
                                time.sleep(random.uniform(0.05, 0.15))
                                driver.execute_cdp_cmd("Input.dispatchMouseEvent", {"type": "mousePressed", "button": "left", "clickCount": 1, "x": int(target_x), "y": int(target_y)})
                                time.sleep(random.uniform(0.04, 0.12))
                                driver.execute_cdp_cmd("Input.dispatchMouseEvent", {"type": "mouseReleased", "button": "left", "clickCount": 1, "x": int(target_x), "y": int(target_y)})
                                _taw_result = True
                                logger.info(f"uc-login: 'Try another way' clicked natively (attempt {_taw_i+1})")
                                break"""

code = re.sub(
    r"                        if _taw_btn and _taw_btn\.is_displayed\(\):\n                            _taw_btn\.click\(\)\n                            _taw_result = True\n                            logger\.info\(f\"uc-login: 'Try another way' clicked natively \(attempt \{_taw_i\+1\}\)\"\)\n                            break",
    replacement_taw,
    code,
    flags=re.DOTALL
)

replacement_sel = """            _sel_js = \"\"\"
(function(){
  var t = document.querySelector('[data-challengetype="6"],[data-challengetype="12"],[data-challengetype="13"]');
  if(!t){
    var els=document.querySelectorAll('div[role="link"],div[role="button"],li,button,div[role="option"],div[role="listitem"],a');
    for(var i=0;i<els.length;i++){
      var txt=(els[i].innerText||els[i].textContent||'').toLowerCase();
      if(txt.includes('authenticator')||txt.includes('auth app')||txt.includes('google auth')||txt.includes('verification app')){
        t=els[i]; break;
      }
    }
  }
  if(t){
    var r = t.getBoundingClientRect();
    return JSON.stringify({found:true, rect:{x:r.x, y:r.y, w:r.width, h:r.height}});
  }
  var items=[];
  document.querySelectorAll('[data-challengetype]').forEach(function(e){items.push(e.getAttribute('data-challengetype')+':'+e.innerText.trim().slice(0,30));});
  return JSON.stringify({found:false, items:items, url:location.pathname});
})()\"\"\"
            _sel_result = None
            import json
            for _sel_i in range(16):   # up to 8s @ 0.5s
                time.sleep(0.5)
                res_str = cdp_eval(_sel_js)
                if res_str:
                    try:
                        res = json.loads(res_str)
                        if res.get("found"):
                            rect = res["rect"]
                            target_x = rect['x'] + rect['w'] * random.uniform(0.3, 0.7)
                            target_y = rect['y'] + rect['h'] * random.uniform(0.3, 0.7)
                            cdp_mouse_move(_mouse_pos["x"], _mouse_pos["y"], target_x, target_y)
                            _mouse_pos["x"], _mouse_pos["y"] = target_x, target_y
                            time.sleep(random.uniform(0.05, 0.15))
                            driver.execute_cdp_cmd("Input.dispatchMouseEvent", {"type": "mousePressed", "button": "left", "clickCount": 1, "x": int(target_x), "y": int(target_y)})
                            time.sleep(random.uniform(0.04, 0.12))
                            driver.execute_cdp_cmd("Input.dispatchMouseEvent", {"type": "mouseReleased", "button": "left", "clickCount": 1, "x": int(target_x), "y": int(target_y)})
                            _sel_result = "selected"
                            logger.info(f"uc-login: authenticator selected natively (attempt {_sel_i+1})")
                            break
                        else:
                            logger.info(f"uc-login: auth select poll [{_sel_i}] → {str(res.get('items'))[:120]}")
                    except Exception as e:
                        logger.debug(f"JSON load failed on _sel_js result: {e}")"""

code = re.sub(
    r"            _sel_js = \"\"\"\n\(function\(\)\{\n  var dispatch=function\(el\)\{\n    el\.scrollIntoView\(\{block:'center'\}\);\n    \['pointerdown','mousedown','pointerup','mouseup','click'\]\.forEach\(function\(ev\)\{\n      el\.dispatchEvent\(new MouseEvent\(ev,\{bubbles:true,cancelable:true,view:window\}\)\);\n    \}\);\n  \};\n\n  // Try known challengetype values for authenticator app(.*?)logger\.warning\(\"uc-login: authenticator not found after 8s\"\)",
    replacement_sel,
    code,
    flags=re.DOTALL
)

with open("colab/xiorun_agent.py", "w") as f:
    f.write(code)
