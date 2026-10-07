with open("colab/xiorun_agent.py") as f:
    code = f.read()

# Replace hardcoded values in _STEALTH_JS with placeholders
code = code.replace("get: () => 'Linux x86_64'", "get: () => '{PLATFORM}'")
code = code.replace("get: () => 1920", "get: () => {SCREEN_W}")
code = code.replace("get: () => 1080", "get: () => {SCREEN_H}")
code = code.replace("get: () => 1040", "get: () => {SCREEN_AH}")
code = code.replace(
    "const _WEBGL_VENDOR   = 'Google Inc. (NVIDIA)';", "const _WEBGL_VENDOR   = '{WEBGL_V}';"
)
code = code.replace(
    "const _WEBGL_RENDERER = 'ANGLE (NVIDIA, NVIDIA GeForce GTX 1050 Direct3D11 vs_5_0 ps_5_0, D3D11)';",
    "const _WEBGL_RENDERER = '{WEBGL_R}';",
)

# Add navigator.userAgentData mock right after User-Agent spoofing block
ua_data_mock = """  // ── 6b. UserAgentData (Client Hints) spoofing ───────────────────────────
  try {
    const _chPlatform = '{CH_PLATFORM}';
    const _mobile = {IS_MOBILE};
    const _brands = [
      {brand: 'Chromium', version: '131'},
      {brand: 'Google Chrome', version: '131'},
      {brand: 'Not_A Brand', version: '24'}
    ];
    Object.defineProperty(navigator, 'userAgentData', {
      get: () => ({
        brands: _brands,
        mobile: _mobile,
        platform: _chPlatform,
        getHighEntropyValues: (hints) => Promise.resolve({
          brands: _brands,
          mobile: _mobile,
          platform: _chPlatform,
          platformVersion: '10.0.0',
          architecture: '{CH_ARCH}',
          model: '',
          bitness: '64',
          fullVersionList: _brands
        })
      }),
      configurable: true
    });
  } catch (_) {}"""

code = code.replace(
    "  try { Object.defineProperty(navigator, 'doNotTrack',          { get: () => null, configurable: true }); } catch (_) {}",
    "  try { Object.defineProperty(navigator, 'doNotTrack',          { get: () => null, configurable: true }); } catch (_) {}\n\n"
    + ua_data_mock,
)

# Fix Date.now() / performance.now()
timing_jitter = """
  // ── Jitter timing APIs to prevent extension measurement ───────────────────
  const _origPerfNow = performance.now;
  performance.now = function() { return _origPerfNow.call(this) + (Math.random() * 0.05); };
  const _origDateNow = Date.now;
  Date.now = function() { return _origDateNow.call(this) + Math.floor(Math.random() * 5); };
"""
code = code.replace(
    "  // ── 1. navigator.webdriver", timing_jitter + "  // ── 1. navigator.webdriver"
)

with open("colab/xiorun_agent.py", "w") as f:
    f.write(code)
