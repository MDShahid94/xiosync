"""Pure domain definitions for PPPoE exit node management.

No I/O, no framework imports (RULE-ARCH-1).
A PPPoEHost represents ONE Mac Mini + its Ubuntu VM.
Multiple hosts can be registered; each contributes up to 981 unique IPs.
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Any


class PPPoESlotState(StrEnum):
    CONNECTING   = "connecting"    # pppd started, waiting for IP
    IDLE         = "idle"          # up + IP assigned, no worker
    ASSIGNED     = "assigned"      # worker routing through this slot
    RECONNECTING = "reconnecting"  # pppd dropped, auto-reconnecting (persist)
    DOWN         = "down"          # pppd not running / macvlan gone
    DESTROYED    = "destroyed"     # macvlan deleted, slot can be re-provisioned


class PPPoEHostState(StrEnum):
    ACTIVE      = "active"        # online, SSH reachable, Tailscale connected
    OFFLINE     = "offline"       # unreachable
    MAINTENANCE = "maintenance"   # temporarily excluded from assignment


class AssignmentStrategy(StrEnum):
    LRU         = "lru"           # least-recently-used idle slot (default)
    ROUND_ROBIN = "round_robin"
    RANDOM      = "random"


# Hard ceilings for a single Airtel residential PPPoE line (verified live)
class PPPoECeilings:
    """Hard limits validated by live 1000-session ground tests on Mac Mini M4."""

    # Per-host (one Mac + one Airtel line)
    MAX_SLOTS_PER_HOST    = 981    # unique IPs before CGNAT pool exhausted
    MAX_CONCURRENT_ACTIVE = 300    # safe concurrent active Chrome sessions (12.8 GB VM)
    RAM_MB_PER_PPPD       = 4      # measured 3.3 MB, rounded up
    RAM_MB_PER_BROWSER    = 200    # conservative Chrome estimate
    VM_RAM_MB             = 12_800
    GPON_WIRE_MBPS        = 500    # physical fiber cap
    BRAS_SESSION_LIMIT    = 1_000  # no limit found in testing; treat as ceiling
    CONNECT_TIMEOUT_S     = 30     # pppd negotiation window
    ROUTE_TIMEOUT_S       = 5      # ip rule add/del latency
    HEALTH_INTERVAL_S     = 60     # background health check cadence
    IP_ROTATION_WAIT_S    = 30     # time for new IP after pppd reconnect
    WARM_POOL_TARGET      = 50     # default idle slots kept ready per host
    WARM_POOL_MINIMUM     = 20     # refill below this count

    # Speed facts (from live test — informational only)
    SPEED_PER_SESSION_BURST_MBPS = 99.0   # BRAS token-bucket burst
    SPEED_PER_SESSION_100C_MBPS  = 10.23  # 100 concurrent sessions
    SPEED_PER_SESSION_1000C_MBPS = 2.08   # 1000 concurrent (OOM-limited, not BRAS)


# Slot → FingerprintProfile name mapping (deterministic by slot index within host)
SLOT_PROFILE_RANGES: list[tuple[int, int, str]] = [
    (0,   199, "Mac M4 Pro"),
    (200, 349, "Mac M2 Air"),
    (350, 449, "Mac M1 Pro"),
    (450, 649, "Windows 11 Intel i7"),
    (650, 749, "Windows 11 AMD Ryzen"),
    (750, 849, "Linux Ubuntu x86"),
    (850, 899, "ChromeOS x86"),
    (900, 980, "Android Samsung S23"),
]


def profile_name_for_slot(slot: int) -> str:
    """Return the fingerprint profile name for a given slot index (0-980)."""
    for lo, hi, name in SLOT_PROFILE_RANGES:
        if lo <= slot <= hi:
            return name
    return "Mac M4 Pro"


@dataclass(frozen=True)
class FingerprintSpec:
    """Immutable fingerprint specification — passed as --device-specs JSON to the worker."""

    name: str
    os: str                # "macos" | "windows" | "linux" | "android"
    cores: int
    ram_gb: int
    webgl_vendor: str
    webgl_renderer: str
    platform: str          # navigator.platform value
    ch_platform: str       # Client Hints Sec-CH-UA-Platform
    ch_version: str        # Client Hints Sec-CH-UA-Platform-Version
    ch_arch: str           # "arm" | "x86"
    screen_width: int
    screen_height: int
    dpr: float             # devicePixelRatio
    cam_name: str          # camera name for enumerateDevices spoof
    is_mobile: bool
    ua_template: str       # {cv} placeholder → Chrome version
    canvas_seed: int       # stable per-slot canvas noise seed
    audio_seed: int        # stable per-slot audio noise seed

    def to_device_specs(self, chrome_version: str) -> dict[str, Any]:
        """Produce the --device-specs JSON consumed by xio-browser.mjs."""
        return {
            "os":             self.os,
            "cores":          self.cores,
            "ram":            self.ram_gb,
            "webgl_renderer": self.webgl_renderer,
            "macos_version":  self.ch_version,
            "width":          self.screen_width,
            "height":         self.screen_height,
            "dpr":            self.dpr,
            "platform":       self.platform,
            "ch_platform":    self.ch_platform,
            "ch_arch":        self.ch_arch,
            "cam_name":       self.cam_name,
            "is_mobile":      self.is_mobile,
            "ua_template":    self.ua_template.replace("{cv}", chrome_version),
            "canvas_seed":    self.canvas_seed,
            "audio_seed":     self.audio_seed,
        }
