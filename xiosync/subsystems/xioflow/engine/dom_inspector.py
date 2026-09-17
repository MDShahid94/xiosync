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
            node_id = item['node_id']
            node_map[node_id] = item

            tag = item['tag']
            text = item['text']

            dom_string_parts.append(f"[{node_id}] <{tag}> \"{text}\"")

        dom_string = "\n".join(dom_string_parts)

        return dom_string, node_map
