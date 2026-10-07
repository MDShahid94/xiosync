import re

with open("xiosync/subsystems/xiogrid/services/pppoe_nodes.py") as f:
    code = f.read()

# 1. Update dataclass
code = re.sub(
    r"(    fingerprint_profile_name: str\n    total_sessions_served: int\n    reconnect_count: int)",
    r"\1\n    fingerprint: dict[str, Any] | None = None",
    code,
)

# 2. Update _to_record
replacement = """    def _to_record(
        self, node: PPPoEExitNode, host: PPPoEHost, profile: FingerprintProfile | None
    ) -> PPPoESlotRecord:
        fingerprint = None
        if profile:
            chrome_version = "131.0.6778.108"
            fingerprint = {
                "os":             profile.os,
                "cores":          profile.cores,
                "ram":            profile.ram_gb,
                "webgl_renderer": profile.webgl_renderer,
                "macos_version":  profile.ch_version,
                "width":          profile.screen_width,
                "height":         profile.screen_height,
                "dpr":            profile.dpr,
                "platform":       profile.platform,
                "ch_platform":    profile.ch_platform,
                "ch_arch":        profile.ch_arch,
                "cam_name":       profile.cam_name,
                "is_mobile":      profile.is_mobile,
                "ua_template":    profile.ua_template.replace("{cv}", chrome_version),
                "canvas_seed":    profile.canvas_seed,
                "audio_seed":     profile.audio_seed,
            }

        return PPPoESlotRecord(
            id=node.id,
            host_id=node.host_id,
            host_name=host.name,
            ppp_slot=node.ppp_slot,
            state=node.state,
            public_ip=node.public_ip,
            cgnat_ip=node.cgnat_ip,
            proxy_url=node.proxy_url,
            proxy_port=node.proxy_port,
            proxy_state=node.proxy_state,
            assigned_worker_ts_ip=node.assigned_worker_ts_ip,
            assigned_session_id=node.assigned_session_id,
            fingerprint_profile_name=profile.name if profile else "unknown",
            total_sessions_served=node.total_sessions_served,
            reconnect_count=node.reconnect_count,
            fingerprint=fingerprint,
        )"""

code = re.sub(
    r"    def _to_record\(\s*self, node: PPPoEExitNode, host: PPPoEHost, profile: FingerprintProfile \| None\s*\) -> PPPoESlotRecord:(.*?)(?=\n    def _fp_record)",
    replacement,
    code,
    flags=re.DOTALL,
)

with open("xiosync/subsystems/xiogrid/services/pppoe_nodes.py", "w") as f:
    f.write(code)
