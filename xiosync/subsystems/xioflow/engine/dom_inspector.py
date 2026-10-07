from __future__ import annotations

import logging

logger = logging.getLogger(__name__)


class DOMInspector:
    """
    JavaScript injection engine for extracting interactive elements from a web page.
    """

    def __init__(self, page):
        """
        Initialize the DOMInspector.

        Args:
            page: Playwright page object.
        """
        self.page = page

    async def get_interactive_elements(self) -> tuple[str, dict[int, dict]]:
        """
        Injects a JS script into the page to find and extract all visible interactive elements.

        Returns:
            A tuple containing:
            - dom_string: compact LLM-friendly format like '[42] <button> "Submit Order"'
            - node_map: dict mapping node_id to element properties
        """
        js_code = """
        () => {
            const elements = document.querySelectorAll('a, button, input, select, textarea, [role="button"], [role="link"], [role="tab"], [role="menuitem"], [role="checkbox"], [role="radio"], [tabindex]:not([tabindex="-1"])');
            const result = [];
            let nodeId = 0;
            
            for (const el of elements) {
                const style = window.getComputedStyle(el);
                if (style.display === 'none' || style.visibility === 'hidden' || style.opacity === '0') {
                    continue;
                }
                
                const rect = el.getBoundingClientRect();
                if (rect.width === 0 || rect.height === 0) {
                    continue;
                }
                
                const vw = Math.max(document.documentElement.clientWidth || 0, window.innerWidth || 0);
                const vh = Math.max(document.documentElement.clientHeight || 0, window.innerHeight || 0);
                
                let selector = '';
                if (el.id) {
                    selector = '#' + el.id;
                } else {
                    let path = [];
                    let current = el;
                    while (current && current !== document.documentElement) {
                        let tag = current.tagName.toLowerCase();
                        let sibling = current;
                        let index = 1;
                        while (sibling.previousElementSibling) {
                            sibling = sibling.previousElementSibling;
                            if (sibling.tagName.toLowerCase() === tag) {
                                index++;
                            }
                        }
                        path.unshift(`${tag}:nth-child(${index})`);
                        current = current.parentElement;
                    }
                    selector = path.join(' > ');
                }
                
                const getXPath = (element) => {
                    if (element.id !== '') return 'id("' + element.id + '")';
                    if (element === document.body) return element.tagName;
                    let ix = 0;
                    let siblings = element.parentNode.childNodes;
                    for (let i = 0; i < siblings.length; i++) {
                        let sibling = siblings[i];
                        if (sibling === element) return getXPath(element.parentNode) + '/' + element.tagName + '[' + (ix + 1) + ']';
                        if (sibling.nodeType === 1 && sibling.tagName === element.tagName) ix++;
                    }
                    return '';
                };
                
                const testId = el.getAttribute('data-testid') || el.getAttribute('data-test-id') || el.getAttribute('data-test') || el.getAttribute('data-cy') || el.getAttribute('data-automation-id') || el.getAttribute('data-qa');
                
                let anchorText = '';
                if (el.labels && el.labels.length > 0) {
                    anchorText = el.labels[0].innerText;
                } else {
                    let prev = el.previousSibling;
                    while (prev && prev.nodeType !== 3) {
                        prev = prev.previousSibling;
                    }
                    if (prev) {
                        anchorText = prev.nodeValue.trim();
                    }
                }
                
                let innerText = (el.innerText || el.value || '').substring(0, 80).replace(/\\n/g, ' ').trim();
                
                result.push({
                    node_id: nodeId++,
                    tag: el.tagName.toLowerCase(),
                    text: innerText,
                    bounding_box: {
                        x: rect.x,
                        y: rect.y,
                        width: rect.width,
                        height: rect.height,
                        nx: rect.x / vw,
                        ny: rect.y / vh,
                        nw: rect.width / vw,
                        nh: rect.height / vh
                    },
                    scrollX: window.scrollX,
                    scrollY: window.scrollY,
                    selector: selector,
                    xpath: getXPath(el),
                    test_id: testId,
                    aria_label: el.getAttribute('aria-label'),
                    anchor_text: anchorText
                });
            }
            return result;
        }
        """

        elements_data = await self.page.evaluate(js_code)

        node_map = {}
        dom_string_parts = []

        for item in elements_data:
            node_id = item["node_id"]
            node_map[node_id] = item

            tag = item["tag"]
            text = item["text"]

            dom_string_parts.append(f'[{node_id}] <{tag}> "{text}"')

        dom_string = "\n".join(dom_string_parts)

        return dom_string, node_map

    async def inspect_target(self, page, selector: str) -> dict:
        """Extract full 9-tier locator data for a single element.

        This is the core capture method used by PageProxy during
        auto-trace.  It runs a JS function on the page that extracts
        all locator strategies for the element matching ``selector``.

        Returns:
            dict with ``place_value`` and ``face_value`` keys matching
            the ``xioflow_memory_nodes`` JSONB schema.  Returns empty
            dicts if the element cannot be found.
        """
        js_code = """(selector) => {
            const el = document.querySelector(selector);
            if (!el) return null;

            const rect = el.getBoundingClientRect();
            const vw = Math.max(document.documentElement.clientWidth || 0, window.innerWidth || 0);
            const vh = Math.max(document.documentElement.clientHeight || 0, window.innerHeight || 0);

            // ── Tier 1: test_id ──────────────────────────────────────
            const testIdAttrs = ['data-testid', 'data-test-id', 'data-test',
                                 'data-cy', 'data-automation-id', 'data-qa'];
            let test_id = null;
            for (const attr of testIdAttrs) {
                const v = el.getAttribute(attr);
                if (v) { test_id = v; break; }
            }

            // ── Tier 2: CSS selector ─────────────────────────────────
            let css_selector = '';
            if (el.id) {
                css_selector = '#' + CSS.escape(el.id);
            } else {
                let path = [];
                let cur = el;
                while (cur && cur !== document.documentElement) {
                    let tag = cur.tagName.toLowerCase();
                    let sib = cur, idx = 1;
                    while (sib.previousElementSibling) {
                        sib = sib.previousElementSibling;
                        if (sib.tagName.toLowerCase() === tag) idx++;
                    }
                    path.unshift(tag + ':nth-of-type(' + idx + ')');
                    cur = cur.parentElement;
                }
                css_selector = path.join(' > ');
            }

            // ── Tier 3: axes_xpath (relative, anchored) ──────────────
            // Walk up to 15 ancestors seeking a stable anchor (ID, test-id).
            // Generate a relative xpath from that anchor down to target.
            let axes_xpath_list = [];
            const buildRelPath = (from, to) => {
                let steps = [];
                let c = to;
                while (c && c !== from) {
                    let tag = c.tagName.toLowerCase();
                    let sib = c, idx = 1;
                    while (sib.previousElementSibling) {
                        sib = sib.previousElementSibling;
                        if (sib.tagName.toLowerCase() === tag) idx++;
                    }
                    steps.unshift(tag + '[' + idx + ']');
                    c = c.parentElement;
                }
                return steps.join('/');
            };
            let ancestor = el.parentElement;
            for (let depth = 0; ancestor && depth < 15; depth++, ancestor = ancestor.parentElement) {
                let anchorAttr = null, anchorVal = null;
                if (ancestor.id) {
                    anchorAttr = 'id';
                    anchorVal = ancestor.id;
                } else {
                    for (const ta of testIdAttrs) {
                        const tv = ancestor.getAttribute(ta);
                        if (tv) { anchorAttr = ta; anchorVal = tv; break; }
                    }
                }
                if (anchorAttr && anchorVal) {
                    const prefix = anchorAttr === 'id'
                        ? '//*[@id="' + anchorVal + '"]'
                        : '//*[@' + anchorAttr + '="' + anchorVal + '"]';
                    const relPath = buildRelPath(ancestor, el);
                    const fullXpath = prefix + '/' + relPath;
                    // Validate uniqueness
                    try {
                        const xr = document.evaluate(fullXpath, document, null,
                            XPathResult.ORDERED_NODE_SNAPSHOT_TYPE, null);
                        const matchCount = xr.snapshotLength;
                        if (matchCount === 1) {
                            axes_xpath_list.push({
                                strategy: anchorAttr === 'id' ? 'ancestor_id' : 'ancestor_testid',
                                xpath: fullXpath,
                                stability: 0.95 - (depth * 0.02),
                                unique: true,
                                depth: depth
                            });
                            break;  // found a unique one, stop
                        }
                    } catch(e) { /* invalid xpath, skip */ }
                }
            }

            // ── Tier 4: absolute xpath ───────────────────────────────
            const getXPath = (element) => {
                if (element.id) return '//*[@id="' + element.id + '"]';
                if (element === document.body) return '//' + element.tagName.toLowerCase();
                let ix = 0;
                let siblings = element.parentNode ? element.parentNode.childNodes : [];
                for (let i = 0; i < siblings.length; i++) {
                    if (siblings[i] === element) {
                        return getXPath(element.parentNode) + '/' +
                               element.tagName.toLowerCase() + '[' + (ix + 1) + ']';
                    }
                    if (siblings[i].nodeType === 1 && siblings[i].tagName === element.tagName) ix++;
                }
                return '';
            };
            const xpath = getXPath(el);

            // ── Tier 5: aria ─────────────────────────────────────────
            const aria = el.getAttribute('aria-label') || el.getAttribute('aria-describedby') || null;

            // ── Tier 6: role + name ──────────────────────────────────
            const role = el.getAttribute('role') || null;
            const name = el.getAttribute('name') || null;

            // ── Tier 7: anchor_text (label / nearby heading) ─────────
            let anchor_text = '';
            if (el.labels && el.labels.length > 0) {
                anchor_text = el.labels[0].innerText || '';
            } else {
                const lbl = el.closest('[aria-labelledby]');
                if (lbl) {
                    const lblEl = document.getElementById(lbl.getAttribute('aria-labelledby'));
                    if (lblEl) anchor_text = lblEl.innerText || '';
                }
            }
            if (!anchor_text) {
                let prev = el.previousElementSibling;
                if (prev && /^(label|span|div|p|h[1-6])$/i.test(prev.tagName)) {
                    anchor_text = (prev.innerText || '').substring(0, 80).trim();
                }
            }

            // ── Tier 8: inner_text ───────────────────────────────────
            const inner_text = (el.innerText || el.value || el.placeholder || '')
                               .substring(0, 80).replace(/\\n/g, ' ').trim();

            // ── Tier 9: coordinates (bounding box, normalized) ───────
            const coordinates = {
                x: Math.round(rect.x),
                y: Math.round(rect.y),
                width: Math.round(rect.width),
                height: Math.round(rect.height),
                nx: +(rect.x / vw).toFixed(4),
                ny: +(rect.y / vh).toFixed(4),
                nw: +(rect.width / vw).toFixed(4),
                nh: +(rect.height / vh).toFixed(4),
                center_x: Math.round(rect.x + rect.width / 2),
                center_y: Math.round(rect.y + rect.height / 2),
            };

            // ── Face value (visual signature) ────────────────────────
            const style = window.getComputedStyle(el);
            const tag = el.tagName.toLowerCase();
            const classes = Array.from(el.classList).slice(0, 10);

            return {
                place_value: {
                    test_id: test_id,
                    aria: aria,
                    axes_xpath: axes_xpath_list.length > 0 ? axes_xpath_list : null,
                    anchor_text: anchor_text || null,
                    inner_text: inner_text || null,
                    selector: css_selector,
                    xpath: xpath,
                    role: role,
                    name: name,
                    coordinates: coordinates,
                },
                face_value: {
                    tag: tag,
                    classes: classes,
                    text: inner_text,
                    bounding_box: coordinates,
                    element_signature: {
                        tag: tag,
                        type: el.getAttribute('type'),
                        placeholder: el.getAttribute('placeholder'),
                        href: tag === 'a' ? el.getAttribute('href') : null,
                        computed_color: style.color,
                        computed_bg: style.backgroundColor,
                        font_size: style.fontSize,
                    },
                },
            };
        }"""

        try:
            result = await page.evaluate(js_code, selector)
        except Exception as e:
            logger.debug("inspect_target failed for %r: %s", selector, e)
            result = None

        if result is None:
            return {"place_value": {}, "face_value": {}}
        return result
