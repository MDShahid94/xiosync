"""Bootstrap seed data for XIOGRID PPPoE subsystem.

Called once from: uv run python -m xiosync.subsystems.xiogrid.seed_pppoe
Or invoked as part of the first-run setup.

Seeds 8 fingerprint profiles (global, shared across all hosts).
Does NOT create any PPPoEHost or PPPoEExitNode records — those are
created at runtime via the /api/v1/pppoe/hosts endpoint.
"""
from __future__ import annotations

import os
import sys

# Allow running as script
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "..", ".."))

from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from xiosync.subsystems.xiogrid.models.exit_node import FingerprintProfile
from xiosync.platform.ids import new_id

_UA_MAC = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/{cv} Safari/537.36"
)
_UA_WIN = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/{cv} Safari/537.36"
)
_UA_LIN = (
    "Mozilla/5.0 (X11; Linux x86_64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/{cv} Safari/537.36"
)
_UA_CRO = (
    "Mozilla/5.0 (X11; CrOS x86_64 14541.0.0) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/{cv} Safari/537.36"
)
_UA_AND = (
    "Mozilla/5.0 (Linux; Android 13; SM-S911B) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/{cv} Mobile Safari/537.36"
)

FINGERPRINT_PROFILES = [
    # Slots 0-199   (200 slots)
    dict(name="Mac M4 Pro",
         os="macos", cores=10, ram_gb=16,
         webgl_vendor="Apple Inc.", webgl_renderer="Apple M4",
         platform="MacIntel", ch_platform="macOS",
         ch_version="15.2.0", ch_arch="arm",
         screen_width=1920, screen_height=1080, dpr=2.0,
         cam_name="FaceTime HD Camera", is_mobile=False,
         ua_template=_UA_MAC, canvas_seed=0x1A2B, audio_seed=0x3C4D),
    # Slots 200-349 (150 slots)
    dict(name="Mac M2 Air",
         os="macos", cores=8, ram_gb=8,
         webgl_vendor="Apple Inc.", webgl_renderer="Apple M2",
         platform="MacIntel", ch_platform="macOS",
         ch_version="14.6.0", ch_arch="arm",
         screen_width=1440, screen_height=900, dpr=2.0,
         cam_name="FaceTime HD Camera", is_mobile=False,
         ua_template=_UA_MAC, canvas_seed=0x5E6F, audio_seed=0x7A8B),
    # Slots 350-449 (100 slots)
    dict(name="Mac M1 Pro",
         os="macos", cores=10, ram_gb=16,
         webgl_vendor="Apple Inc.", webgl_renderer="Apple M1 Pro",
         platform="MacIntel", ch_platform="macOS",
         ch_version="13.7.0", ch_arch="arm",
         screen_width=1920, screen_height=1080, dpr=2.0,
         cam_name="FaceTime HD Camera", is_mobile=False,
         ua_template=_UA_MAC, canvas_seed=0x9CAD, audio_seed=0xBECF),
    # Slots 450-649 (200 slots)
    dict(name="Windows 11 Intel i7",
         os="windows", cores=8, ram_gb=16,
         webgl_vendor="Google Inc. (Intel)",
         webgl_renderer=(
             "ANGLE (Intel, Intel(R) UHD Graphics 620 "
             "Direct3D11 vs_5_0 ps_5_0, D3D11)"
         ),
         platform="Win32", ch_platform="Windows",
         ch_version="10.0.0", ch_arch="x86",
         screen_width=1920, screen_height=1080, dpr=1.0,
         cam_name="Integrated Webcam", is_mobile=False,
         ua_template=_UA_WIN, canvas_seed=0xD0E1, audio_seed=0xF2A3),
    # Slots 650-749 (100 slots)
    dict(name="Windows 11 AMD Ryzen",
         os="windows", cores=12, ram_gb=32,
         webgl_vendor="Google Inc. (AMD)",
         webgl_renderer=(
             "ANGLE (AMD, AMD Radeon Graphics (0x00001681) "
             "Direct3D11 vs_5_0 ps_5_0, D3D11)"
         ),
         platform="Win32", ch_platform="Windows",
         ch_version="10.0.0", ch_arch="x86",
         screen_width=2560, screen_height=1440, dpr=1.0,
         cam_name="Integrated Webcam", is_mobile=False,
         ua_template=_UA_WIN, canvas_seed=0xB4C5, audio_seed=0xD6E7),
    # Slots 750-849 (100 slots)
    dict(name="Linux Ubuntu x86",
         os="linux", cores=4, ram_gb=8,
         webgl_vendor="Mesa/X.org",
         webgl_renderer="llvmpipe (LLVM 15.0.7, 256 bits)",
         platform="Linux x86_64", ch_platform="Linux",
         ch_version="5.15.0", ch_arch="x86",
         screen_width=1920, screen_height=1080, dpr=1.0,
         cam_name="USB2.0 Camera", is_mobile=False,
         ua_template=_UA_LIN, canvas_seed=0xF8A9, audio_seed=0x1B2C),
    # Slots 850-899 (50 slots)
    dict(name="ChromeOS x86",
         os="linux", cores=4, ram_gb=8,
         webgl_vendor="Google Inc. (Intel)",
         webgl_renderer=(
             "ANGLE (Intel, Mesa Intel(R) HD Graphics 620, OpenGL 4.6)"
         ),
         platform="Linux x86_64", ch_platform="Chrome OS",
         ch_version="119.0.6045", ch_arch="x86",
         screen_width=1366, screen_height=768, dpr=1.0,
         cam_name="HD Webcam C270", is_mobile=False,
         ua_template=_UA_CRO, canvas_seed=0x3D4E, audio_seed=0x5F60),
    # Slots 900-980 (81 slots)
    dict(name="Android Samsung S23",
         os="android", cores=8, ram_gb=8,
         webgl_vendor="Qualcomm",
         webgl_renderer="Adreno (TM) 740",
         platform="Linux armv81", ch_platform="Android",
         ch_version="13.0.0", ch_arch="arm",
         screen_width=393, screen_height=873, dpr=3.0,
         cam_name="Front Camera", is_mobile=True,
         ua_template=_UA_AND, canvas_seed=0x7182, audio_seed=0x9304),
]


def seed_fingerprint_profiles(session: Session) -> int:
    """Insert missing fingerprint profiles. Idempotent (skips existing names)."""
    inserted = 0
    for data in FINGERPRINT_PROFILES:
        exists = session.scalar(
            select(FingerprintProfile).where(
                FingerprintProfile.name == data["name"]
            )
        )
        if not exists:
            session.add(FingerprintProfile(id=new_id(), **data))
            inserted += 1
    session.flush()
    return inserted


if __name__ == "__main__":
    db_url = os.environ.get("DATABASE_URL")
    if not db_url:
        print("ERROR: DATABASE_URL not set", file=sys.stderr)
        sys.exit(1)
    engine = create_engine(db_url)
    with Session(engine) as s:
        n = seed_fingerprint_profiles(s)
        s.commit()
    print(f"Seeded {n} fingerprint profiles (skipped existing)")
