import re

with open("tools/workflows/google-signin.mjs") as f:
    code = f.read()

replacement = """      const req = request({
        hostname: fullUrl.hostname,
        port:     fullUrl.port || (isHttps ? 443 : 80),
        path:     fullUrl.pathname,
        method:   'POST',
        headers:  {
          'Content-Type': 'application/json',
          'X-Worker-Secret': process.env.WORKER_SECRET || '',
        },
      }, res => {"""

code = re.sub(
    r"      const req = request\(\{\n        hostname: fullUrl\.hostname,\n        port:     fullUrl\.port \|\| \(isHttps \? 443 : 80\),\n        path:     fullUrl\.pathname,\n        method:   'POST',\n        headers:  \{ 'Content-Type': 'application/json' \},\n      \}, res => \{",
    replacement,
    code,
    flags=re.DOTALL,
)

with open("tools/workflows/google-signin.mjs", "w") as f:
    f.write(code)
