import re

with open("colab/xiorun_agent.py", "r") as f:
    code = f.read()

replacement = """    _fp = req.fingerprint or {}
    _canvas_seed = _fp.get("canvas_seed", 0x1A2B)
    _audio_seed  = _fp.get("audio_seed",  0x3C4D)
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
    _ua_str = _fp.get("ua_template", _STEALTH_UA_CHROME)
    
    # Compute TZ offset: minutes west of UTC (e.g. IST UTC+5:30 → -330)
    import datetime as _dt
    try:
        import zoneinfo as _zi
        _tz_obj    = _zi.ZoneInfo(_timezone)
        _tz_offset = -int(_dt.datetime.now(_tz_obj).utcoffset().total_seconds() // 60)
    except Exception:
        _tz_offset = 0
    _session_stealth_js = (
        _STEALTH_JS
        .replace("'{TIMEZONE}'",  f"'{_timezone}'")
        .replace("{TIMEZONE}",    _timezone)
        .replace("'{LOCALE}'",    f"'{_locale}'")
        .replace("{LOCALE}",      _locale)
        .replace("{CANVAS_SEED}", str(_canvas_seed))
        .replace("{AUDIO_SEED}",  str(_audio_seed))
        .replace("{LAT}",         str(_geo_lat))
        .replace("{LON}",         str(_geo_lon))
        .replace("'{UA}'",        f"'{_ua_str}'")
        .replace("{UA}",          _ua_str)
        .replace("{TZ_OFFSET}",   str(_tz_offset))
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
    r"    _canvas_seed = req\.fingerprint\.get\(\"canvas_seed\", 0x1A2B\)\n    _audio_seed  = req\.fingerprint\.get\(\"audio_seed\",  0x3C4D\)\n    # Compute TZ offset: minutes west of UTC \(e\.g\. IST UTC\+5:30 → -330\)\n    import datetime as _dt\n    try:\n        import zoneinfo as _zi\n        _tz_obj    = _zi\.ZoneInfo\(_timezone\)\n        _tz_offset = -int\(_dt\.datetime\.now\(_tz_obj\)\.utcoffset\(\)\.total_seconds\(\) // 60\)\n    except Exception:\n        _tz_offset = 0\n    _ua_for_js = _STEALTH_UA_CHROME  # Chrome 131 UA matching our binary\n    _session_stealth_js = \(\n        _STEALTH_JS(.*?)\)\n",
    replacement + "\n",
    code,
    flags=re.DOTALL
)

with open("colab/xiorun_agent.py", "w") as f:
    f.write(code)
