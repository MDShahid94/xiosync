import re

with open("colab/xiorun_agent.py", "r") as f:
    code = f.read()

replacement = """            # Use fingerprint if provided, else fallback
            _fp = fingerprint or {}
            _uc_canvas_seed = _fp.get("canvas_seed", random.randint(0x1000, 0xFFFF))
            _uc_audio_seed  = _fp.get("audio_seed", random.randint(0x1000, 0xFFFF))
            _cores = str(_fp.get("cores", os.cpu_count() or 2))
            _ram = str(_fp.get("ram", max(2, min(8, (os.cpu_count() or 2) * 2))))
            _platform = _fp.get("platform", "Linux x86_64")
            _ch_platform = _fp.get("ch_platform", "Linux")
            _ch_arch = _fp.get("ch_arch", "x86")
            _is_mobile = "true" if _fp.get("is_mobile") else "false"
            _screen_w = str(_fp.get("width", 1920))
            _screen_h = str(_fp.get("height", 1080))
            _screen_ah = str(max(100, int(_screen_h) - 40))
            _webgl_v = "Google Inc. (NVIDIA)"
            _webgl_r = _fp.get("webgl_renderer", "ANGLE (NVIDIA, NVIDIA GeForce GTX 1050 Direct3D11 vs_5_0 ps_5_0, D3D11)")
            if "Apple" in _webgl_r: _webgl_v = "Apple"
            elif "Intel" in _webgl_r: _webgl_v = "Google Inc. (Intel)"
            elif "AMD" in _webgl_r: _webgl_v = "Google Inc. (AMD)"
            _ua_str = _fp.get("ua_template", user_agent)
            
            _uc_stealth = (
                _STEALTH_JS
                .replace("'{TIMEZONE}'",  f"'{_uc_timezone}'")
                .replace("{TIMEZONE}",    _uc_timezone)
                .replace("'{LOCALE}'",    f"'{_uc_locale}'")
                .replace("{LOCALE}",      _uc_locale)
                .replace("{CANVAS_SEED}", str(_uc_canvas_seed))
                .replace("{AUDIO_SEED}",  str(_uc_audio_seed))
                .replace("{LAT}",         str(_uc_lat))
                .replace("{LON}",         str(_uc_lon))
                .replace("'{UA}'",        f"'{_ua_str}'")
                .replace("{UA}",          _ua_str)
                .replace("{TZ_OFFSET}",   str(_tz_off2))
                .replace("{CPU_COUNT}",   _cores)
                .replace("{DEVICE_MEMORY_GB}", _ram)
                .replace("{PLATFORM}",    _platform)
                .replace("{CH_PLATFORM}", _ch_platform)
                .replace("{CH_ARCH}",     _ch_arch)
                .replace("{IS_MOBILE}",   _is_mobile)
                .replace("{SCREEN_W}",    _screen_w)
                .replace("{SCREEN_H}",    _screen_h)
                .replace("{SCREEN_AH}",   _screen_ah)
                .replace("{WEBGL_V}",     _webgl_v)
                .replace("{WEBGL_R}",     _webgl_r)
            )"""

code = re.sub(
    r"            _uc_canvas_seed = random\.randint\(0x1000, 0xFFFF\)\n            _uc_audio_seed  = random\.randint\(0x1000, 0xFFFF\)\n            _uc_stealth = \(\n                _STEALTH_JS(.*?)\)\n",
    replacement + "\n",
    code,
    flags=re.DOTALL
)

with open("colab/xiorun_agent.py", "w") as f:
    f.write(code)
