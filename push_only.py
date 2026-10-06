import urllib.request
import json
import urllib.error
import urllib.parse

WORKER_TS_IP = "100.111.130.118"
WORKER_AGENT = f"http://{WORKER_TS_IP}:9300"
TOKEN        = "xiosync-internal-2026-karma"

body = {
    "identity_id": "karmareturnsfromallsides",
    "local_dir": "/tmp/xiorun_profiles/PRFL_karmareturnsfromallsides__xiogrid__default__worker_006",
    "drive_object_key": "profiles/PRFL-001.tar.gz",
}

req = urllib.request.Request(
    f"{WORKER_AGENT}/push-profile",
    data=json.dumps(body).encode(),
    headers={
        "Content-Type": "application/json",
        "Authorization": f"Bearer {TOKEN}"
    },
    method="POST"
)

try:
    with urllib.request.urlopen(req, timeout=120) as r:
        resp = json.loads(r.read().decode())
        print(f"✅ Profile pushed: {resp}")
except urllib.error.HTTPError as e:
    print(f"❌ Error: {e.code} {e.read().decode()}")
