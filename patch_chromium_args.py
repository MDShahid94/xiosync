import re

with open("colab/xiorun_agent.py") as f:
    code = f.read()

# 1. Update CHROMIUM_ARGS
replacement_chromium_args = """CHROMIUM_ARGS = [
    "--no-sandbox",
    "--disable-setuid-sandbox",
    "--disable-dev-shm-usage",
    "--disable-gpu-sandbox",
    "--ignore-gpu-blocklist",
    "--disable-blink-features=AutomationControlled",
    "--disable-infobars",
    "--remote-debugging-address=0.0.0.0",  # bind to Tailscale IP
    # Anti-detection: language + feature flags
    "--lang=en-US,en",
    "--accept-lang=en-US,en;q=0.9",
    # Needed for real Chrome on Xvfb (no real GPU)
    "--use-gl=swiftshader",
    "--disable-software-rasterizer",
    "--disable-automation",
    "--exclude-switches=enable-automation",
]"""

code = re.sub(r"CHROMIUM_ARGS = \[(.*?)\n\]", replacement_chromium_args, code, flags=re.DOTALL)

# 2. Update UC opts
# Remove UserAgentClientHint
code = code.replace(
    'opts.add_argument("--disable-features=ServiceWorker,UserAgentClientHint")',
    'opts.add_argument("--disable-features=ServiceWorker")',
)

# Remove explicit user-agent flag to avoid mismatch - we'll handle this in CDP
code = code.replace('opts.add_argument(f"--user-agent={user_agent}")\n', "")

# 3. Add useAutomationExtension=false and excludeSwitches
replacement_prefs = """      opts.add_experimental_option("excludeSwitches", ["enable-automation"])
      opts.add_experimental_option("useAutomationExtension", False)
      opts.add_experimental_option("prefs", {"""
code = code.replace('      opts.add_experimental_option("prefs", {', replacement_prefs)

with open("colab/xiorun_agent.py", "w") as f:
    f.write(code)
