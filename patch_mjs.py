import re

with open("tools/workflows/google-signin.mjs", "r") as f:
    code = f.read()

replacement = """  const httpPost = async (baseUrl, path, data, timeoutMs = 60000) => {
    const fullUrl = new URL(`${baseUrl}${path}`);
    const isHttps = fullUrl.protocol === 'https:';
    const { request } = await import(isHttps ? 'node:https' : 'node:http');
    return new Promise((resolve, reject) => {
      const req = request({
        hostname: fullUrl.hostname,
        port:     fullUrl.port || (isHttps ? 443 : 80),
        path:     fullUrl.pathname,
        method:   'POST',
        headers:  {
          'Content-Type': 'application/json',
          'X-Worker-Secret': process.env.WORKER_SECRET || '',
        },
        timeout:  timeoutMs,
      }, res => {"""

code = re.sub(
    r"  const httpPost = async \(baseUrl, path, data, timeoutMs = 60000\) => \{\n    const fullUrl = new URL\(`\$\{baseUrl\}\$\{path\}`\);\n    const isHttps = fullUrl\.protocol === 'https:';\n    const \{ request \} = await import\(isHttps \? 'node:https' : 'node:http'\);\n    return new Promise\(\(resolve, reject\) => \{\n      const req = request\(\{\n        hostname: fullUrl\.hostname,\n        port:     fullUrl\.port \|\| \(isHttps \? 443 : 80\),\n        path:     fullUrl\.pathname,\n        method:   'POST',\n        headers:  \{ 'Content-Type': 'application/json' \},\n        timeout:  timeoutMs,\n      \}, res => \{",
    replacement,
    code,
    flags=re.DOTALL
)

with open("tools/workflows/google-signin.mjs", "w") as f:
    f.write(code)
