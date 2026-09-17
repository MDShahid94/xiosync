from __future__ import annotations

import base64

import numpy as np
import structlog

logger = structlog.get_logger(__name__)

class VisionMatcher:
    """OpenCV template matching for Tier 9."""

    @staticmethod
    def match_template(full_page_b64: str, template_b64: str, threshold: float = 0.8) -> dict:
        """Match template using cv2."""
        try:
            import cv2
        except ImportError:
            logger.warning("cv2_import_failed")
            return {"success": False, "x": 0, "y": 0, "confidence": 0.0}

        try:
            img_data = base64.b64decode(full_page_b64)
            tmpl_data = base64.b64decode(template_b64)

            nparr_img = np.frombuffer(img_data, np.uint8)
            nparr_tmpl = np.frombuffer(tmpl_data, np.uint8)

            img = cv2.imdecode(nparr_img, cv2.IMREAD_COLOR)
            template = cv2.imdecode(nparr_tmpl, cv2.IMREAD_COLOR)

            if img is None or template is None:
                return {"success": False, "x": 0, "y": 0, "confidence": 0.0}

            res = cv2.matchTemplate(img, template, cv2.TM_CCOEFF_NORMED)
            min_val, max_val, min_loc, max_loc = cv2.minMaxLoc(res)

            if max_val >= threshold:
                h, w = template.shape[:2]
                center_x = max_loc[0] + w // 2
                center_y = max_loc[1] + h // 2
                return {"success": True, "x": center_x, "y": center_y, "confidence": max_val}

            return {"success": False, "x": 0, "y": 0, "confidence": max_val}

        except Exception as e:
            logger.error("vision_matcher_error", error=str(e))
            return {"success": False, "x": 0, "y": 0, "confidence": 0.0}
