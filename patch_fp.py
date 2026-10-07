with open("colab/xiorun_agent.py") as f:
    code = f.read()

# The error happens in `def _run_uc_login_impl(...)` around line 2087:
# 2085:        try:
# 2086:            # Build CDP UA override dynamically
# 2087:            _ch_platform = _fp.get("ch_platform", "Linux")

# I'll just change `_fp` to `(fingerprint or {})` in the UA override section to avoid unbound local error.

code = code.replace(
    '_ch_platform = _fp.get("ch_platform", "Linux")',
    '_fp_ua = fingerprint or {}; _ch_platform = _fp_ua.get("ch_platform", "Linux")',
)
code = code.replace(
    '_ch_arch = _fp.get("ch_arch", "x86")', '_ch_arch = _fp_ua.get("ch_arch", "x86")'
)
code = code.replace(
    '_is_mobile = bool(_fp.get("is_mobile", False))',
    '_is_mobile = bool(_fp_ua.get("is_mobile", False))',
)
code = code.replace(
    '_ua_str = _fp.get("ua_template", user_agent)',
    '_ua_str = _fp_ua.get("ua_template", user_agent)',
)

with open("colab/xiorun_agent.py", "w") as f:
    f.write(code)
