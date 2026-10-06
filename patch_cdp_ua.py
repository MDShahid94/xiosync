import re

with open("colab/xiorun_agent.py", "r") as f:
    code = f.read()

replacement = """            # Build CDP UA override dynamically
            _ch_platform = _fp.get("ch_platform", "Linux")
            _ch_arch = _fp.get("ch_arch", "x86")
            _is_mobile = bool(_fp.get("is_mobile", False))
            _ua_str = _fp.get("ua_template", user_agent)
            
            driver.execute_cdp_cmd(
                "Network.setUserAgentOverride",
                {
                    "userAgent":         _ua_str,
                    "acceptLanguage":    f"{_uc_locale},{_uc_locale.split('-')[0]};q=0.9,en;q=0.8",
                    "platform":          _ch_platform,
                    "userAgentMetadata": {
                        "brands": [
                            {"brand": "Google Chrome",   "version": "131"},
                            {"brand": "Chromium",        "version": "131"},
                            {"brand": "Not_A Brand",     "version": "24"},
                        ],
                        "fullVersion":    "131.0.6778.264",
                        "platform":       _ch_platform,
                        "platformVersion":"10.0.0",
                        "architecture":   _ch_arch,
                        "model":          "",
                        "mobile":         _is_mobile,
                        "bitness":        "64",
                        "wow64":          False,
                    },
                },
            )"""

code = re.sub(
    r"            driver\.execute_cdp_cmd\(\n                \"Network\.setUserAgentOverride\",(.*?)\"wow64\":          False,\n                    \},\n                \},\n            \)",
    replacement,
    code,
    flags=re.DOTALL
)

with open("colab/xiorun_agent.py", "w") as f:
    f.write(code)
