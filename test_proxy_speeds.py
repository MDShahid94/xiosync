import os
import sys
import time
import json
import subprocess
import concurrent.futures
from statistics import mean, median
from xiosync.persistence.database import create_database_engine
from sqlalchemy.orm import Session
from sqlalchemy import select
from xiosync.subsystems.xiogrid.models.exit_node import PPPoEExitNode

url = os.environ.get("XIOSYNC_DATABASE_URL", "postgresql+psycopg://postgres:postgres@localhost:5432/xiosync")
engine = create_database_engine(url)

with Session(engine) as db:
    nodes = db.scalars(select(PPPoEExitNode).where(PPPoEExitNode.state.in_(["idle", "reconnecting"]))).all()
    active_proxies = [{"slot": n.ppp_slot, "ip": n.public_ip, "proxy": n.proxy_url} for n in nodes if n.proxy_url]

print(f"Found {len(active_proxies)} active proxies to test.")

# 5 MB file for speed test
DOWNLOAD_URL = "https://speed.cloudflare.com/__down?bytes=5000000"
UPLOAD_URL = "https://speed.cloudflare.com/__up"
# Create a 2MB dummy file for upload testing
with open("/tmp/dummy_upload.dat", "wb") as f:
    f.write(os.urandom(2000000))

def test_speed(proxy_info):
    slot = proxy_info["slot"]
    ip = proxy_info["ip"]
    proxy = proxy_info["proxy"]
    
    result = {"slot": slot, "ip": ip, "dl_mbps": 0.0, "ul_mbps": 0.0, "error": None}
    
    try:
        # Replace socks5:// with socks5h:// to force remote DNS
        proxy = proxy.replace("socks5://", "socks5h://")
        
        # Download Test
        dl_cmd = [
            "curl", "-4", "-s", "-w", "%{speed_download}", "-o", "/dev/null",
            "-x", proxy, "--max-time", "15", DOWNLOAD_URL
        ]
        dl_out = subprocess.check_output(dl_cmd, text=True).strip()
        dl_bps = float(dl_out)  # bytes per second
        result["dl_mbps"] = (dl_bps * 8) / 1_000_000
        
        # Upload Test
        ul_cmd = [
            "curl", "-4", "-s", "-w", "%{speed_upload}", "-o", "/dev/null",
            "-x", proxy, "-F", "file=@/tmp/dummy_upload.dat", "--max-time", "15", UPLOAD_URL
        ]
        ul_out = subprocess.check_output(ul_cmd, text=True).strip()
        ul_bps = float(ul_out)  # bytes per second
        result["ul_mbps"] = (ul_bps * 8) / 1_000_000
        
    except Exception as e:
        result["error"] = str(e)
        
    return result

results = []
# Concurrency 15 to balance test speed and network saturation (testing all 160 concurrently would crash the host's link)
print("Starting concurrent speed tests (batching 15 at a time to prevent link saturation)...")
with concurrent.futures.ThreadPoolExecutor(max_workers=15) as executor:
    futs = [executor.submit(test_speed, p) for p in active_proxies]
    for i, fut in enumerate(concurrent.futures.as_completed(futs)):
        r = fut.result()
        results.append(r)
        if r["error"]:
            print(f"[{i+1}/{len(active_proxies)}] Slot {r['slot']} ({r['ip']}): FAILED")
        else:
            print(f"[{i+1}/{len(active_proxies)}] Slot {r['slot']} ({r['ip']}): DL {r['dl_mbps']:.2f} Mbps | UL {r['ul_mbps']:.2f} Mbps")

# Summary
successful = [r for r in results if not r["error"] and r["dl_mbps"] > 0]
failed = [r for r in results if r["error"] or r["dl_mbps"] == 0]

with open("/Users/karmareturns/.gemini/antigravity/brain/02eaec7a-24b9-4a3b-a6ba-9b852cbd8265/speedtest_summary.md", "w") as f:
    f.write("# Proxy Speedtest Summary\n\n")
    f.write(f"**Total Tested:** {len(results)}\n")
    f.write(f"**Successful:** {len(successful)}\n")
    f.write(f"**Failed / Timed Out:** {len(failed)}\n\n")
    
    if successful:
        dl_speeds = [r["dl_mbps"] for r in successful]
        ul_speeds = [r["ul_mbps"] for r in successful]
        f.write(f"**Average Download:** {mean(dl_speeds):.2f} Mbps (Max: {max(dl_speeds):.2f} Mbps)\n")
        f.write(f"**Average Upload:** {mean(ul_speeds):.2f} Mbps (Max: {max(ul_speeds):.2f} Mbps)\n\n")
        f.write("| Slot | Public IP | Download (Mbps) | Upload (Mbps) |\n")
        f.write("|---|---|---|---|\n")
        for r in sorted(successful, key=lambda x: x["dl_mbps"], reverse=True):
            f.write(f"| {r['slot']} | {r['ip']} | {r['dl_mbps']:.2f} | {r['ul_mbps']:.2f} |\n")

if successful:
    print("\n--- SPEED TEST SUMMARY ---")
    print(f"Successful: {len(successful)} | Failed: {len(failed)}")
    print(f"Average Download: {mean(dl_speeds):.2f} Mbps")
    print(f"Average Upload:   {mean(ul_speeds):.2f} Mbps")
