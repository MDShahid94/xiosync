"""CDP native behavioral interaction primitives and humanized input helpers.

Extracts CDP input automation routines (Bezier mouse movement, human-like typing,
input clearing, evaluation, and sleeping) from inside Selenium/Undetected-Chromedriver
contexts into standalone functions accepting driver as first argument.
"""

from __future__ import annotations

import logging
import random
import time
from typing import Any

logger = logging.getLogger("xiorun_agent.browser.cdp_primitives")

_DEFAULT_MOUSE_POS: dict[str, int | float] = {"x": 100, "y": 100}


def _bezier(
    p0: tuple[float, float],
    p1: tuple[float, float],
    p2: tuple[float, float],
    p3: tuple[float, float],
    t: float,
) -> tuple[float, float]:
    """Calculate cubic Bezier curve point at parameter t."""
    x = (
        (1 - t) ** 3 * p0[0]
        + 3 * (1 - t) ** 2 * t * p1[0]
        + 3 * (1 - t) * t**2 * p2[0]
        + t**3 * p3[0]
    )
    y = (
        (1 - t) ** 3 * p0[1]
        + 3 * (1 - t) ** 2 * t * p1[1]
        + 3 * (1 - t) * t**2 * p2[1]
        + t**3 * p3[1]
    )
    return x, y


def cdp_mouse_move(
    driver: Any,
    start_x: float,
    start_y: float,
    end_x: float,
    end_y: float,
) -> None:
    """Simulate human-like curved mouse movement using Bezier curves via CDP."""
    steps = random.randint(12, 25)
    cx1 = start_x + (end_x - start_x) * random.uniform(0.2, 0.8) + random.uniform(-30, 30)
    cy1 = start_y + (end_y - start_y) * random.uniform(0.2, 0.8) + random.uniform(-30, 30)
    cx2 = start_x + (end_x - start_x) * random.uniform(0.2, 0.8) + random.uniform(-30, 30)
    cy2 = start_y + (end_y - start_y) * random.uniform(0.2, 0.8) + random.uniform(-30, 30)
    for i in range(steps + 1):
        t = i / steps
        x, y = _bezier((start_x, start_y), (cx1, cy1), (cx2, cy2), (end_x, end_y), t)
        driver.execute_cdp_cmd(
            "Input.dispatchMouseEvent", {"type": "mouseMoved", "x": int(x), "y": int(y)}
        )
        time.sleep(random.uniform(0.005, 0.015))


def cdp_get_rect(driver: Any, selector: str) -> dict[str, float] | None:
    """Get element bounding rect via untracked CDP Runtime.evaluate."""
    js = f"""
    (function() {{
        var el = document.querySelector('{selector}');
        if (!el) return null;
        var rect = el.getBoundingClientRect();
        return {{x: rect.x, y: rect.y, w: rect.width, h: rect.height}};
    }})()
    """
    try:
        res = driver.execute_cdp_cmd(
            "Runtime.evaluate", {"expression": js, "returnByValue": True}
        )
        return res.get("result", {}).get("value")
    except Exception:
        return None


def cdp_click_element(
    driver: Any,
    selectors: str | list[str],
    timeout: float = 10,
    mouse_pos: dict[str, int | float] | None = None,
) -> bool:
    """Click an element using human-like mouse curve trajectory and timing."""
    if isinstance(selectors, str):
        selectors = [selectors]
    rect = None
    for _ in range(int(timeout * 2.5)):
        for sel in selectors:
            rect = cdp_get_rect(driver, sel)
            if rect and rect.get("w", 0) > 0 and rect.get("h", 0) > 0:
                break
        if rect and rect.get("w", 0) > 0 and rect.get("h", 0) > 0:
            break
        time.sleep(0.4)
    if not rect or rect.get("w", 0) <= 0:
        raise Exception(f"Element not found for CDP click: {selectors}")

    pos = mouse_pos if mouse_pos is not None else _DEFAULT_MOUSE_POS

    # ── Pre-interaction Scrolling Seasoning ──
    if random.random() < 0.4:
        scroll_dir = random.choice([100, 200, -100])
        driver.execute_cdp_cmd(
            "Input.dispatchMouseEvent",
            {
                "type": "mouseWheel",
                "x": int(pos["x"]),
                "y": int(pos["y"]),
                "deltaX": 0,
                "deltaY": scroll_dir,
            },
        )
        time.sleep(random.uniform(0.1, 0.3))
        driver.execute_cdp_cmd(
            "Input.dispatchMouseEvent",
            {
                "type": "mouseWheel",
                "x": int(pos["x"]),
                "y": int(pos["y"]),
                "deltaX": 0,
                "deltaY": -scroll_dir,
            },
        )
        time.sleep(random.uniform(0.1, 0.3))

    target_x = rect["x"] + rect["w"] * random.uniform(0.3, 0.7)
    target_y = rect["y"] + rect["h"] * random.uniform(0.3, 0.7)

    cdp_mouse_move(driver, pos["x"], pos["y"], target_x, target_y)
    pos["x"], pos["y"] = target_x, target_y

    time.sleep(random.uniform(0.05, 0.15))
    driver.execute_cdp_cmd(
        "Input.dispatchMouseEvent",
        {
            "type": "mousePressed",
            "button": "left",
            "clickCount": 1,
            "x": int(target_x),
            "y": int(target_y),
        },
    )
    time.sleep(random.uniform(0.04, 0.12))
    driver.execute_cdp_cmd(
        "Input.dispatchMouseEvent",
        {
            "type": "mouseReleased",
            "button": "left",
            "clickCount": 1,
            "x": int(target_x),
            "y": int(target_y),
        },
    )
    return True


def cdp_type_text(driver: Any, text: str) -> None:
    """Type text into active input element, compatible with React form state."""
    js_inject = f"""
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
    """
    driver.execute_script(js_inject)
    time.sleep(random.uniform(0.1, 0.3))


def cdp_clear_input(driver: Any) -> None:
    """Clear active input field and simulate backspace keystroke."""
    js_inject = """
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
    """
    driver.execute_script(js_inject)
    time.sleep(0.1)
    driver.execute_cdp_cmd(
        "Input.dispatchKeyEvent",
        {"type": "keyDown", "windowsVirtualKeyCode": 8, "key": "Backspace"},
    )
    driver.execute_cdp_cmd(
        "Input.dispatchKeyEvent",
        {"type": "keyUp", "windowsVirtualKeyCode": 8, "key": "Backspace"},
    )
    time.sleep(0.05)


def cdp_eval(driver: Any, js: str, await_promise: bool = False) -> Any:
    """Execute JS via CDP Runtime.evaluate — undetectable by anti-bot checks."""
    try:
        result = driver.execute_cdp_cmd(
            "Runtime.evaluate",
            {
                "expression": js,
                "awaitPromise": await_promise,
                "returnByValue": True,
                "timeout": 8000,
            },
        )
        return result.get("result", {}).get("value")
    except Exception as ce:
        logger.warning(f"uc-login: cdp_eval error: {ce}")
        return None


def uc_sleep(
    driver: Any = None,
    a: float = 0.8,
    b: float = 2.0,
    mouse_pos: dict[str, int | float] | None = None,
) -> None:
    """Sleep for randomized duration with optional idle mouse jitter."""
    pos = mouse_pos if mouse_pos is not None else _DEFAULT_MOUSE_POS
    duration = a + random.random() * (b - a)
    end_time = time.time() + duration
    while time.time() < end_time:
        # ── Idle Jitter Seasoning ──
        if driver is not None and random.random() < 0.35:
            tgt_x = max(10, min(1900, pos["x"] + random.randint(-80, 80)))
            tgt_y = max(10, min(1000, pos["y"] + random.randint(-80, 80)))
            try:
                cdp_mouse_move(driver, pos["x"], pos["y"], tgt_x, tgt_y)
                pos["x"], pos["y"] = tgt_x, tgt_y
            except Exception:
                pass
        rem = end_time - time.time()
        if rem <= 0:
            break
        time.sleep(min(rem, random.uniform(0.1, 0.4)))


def safe_get(
    driver: Any,
    url: str,
    wait: float = 1.0,
    mouse_pos: dict[str, int | float] | None = None,
) -> bool:
    """Navigate to url; return True on success, False on timeout/error.
    Never raises — callers check the return value and decide whether to continue.
    """
    try:
        from selenium.common.exceptions import TimeoutException as _SeleniumTimeout
    except ImportError:
        _SeleniumTimeout = Exception  # type: ignore[misc,assignment]

    try:
        driver.get(url)
        if wait > 0:
            uc_sleep(driver, wait * 0.5, wait, mouse_pos=mouse_pos)
        return True
    except _SeleniumTimeout:
        logger.warning(f"uc-login: page load timed out ({url[:60]}) — continuing")
        return False
    except Exception as _nav_err:
        logger.warning(f"uc-login: navigation error ({url[:60]}): {_nav_err}")
        return False
