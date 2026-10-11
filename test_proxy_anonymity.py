import concurrent.futures
import os
import subprocess
import json
import random

from sqlalchemy import select
from sqlalchemy.orm import Session
from xiosync.persistence.database import create_database_engine
from xiosync.subsystems.xiogrid.models.exit_node import PPPoEExitNode

url = os.environ.get("XIOSYNC_DATABASE_URL", "postgresql+psycopg://postgres:postgres@localhost:5432/xiosync")
engine = create_database_engine(url)

with Session(engine) as db:
    nodes = db.scalars(select(PPPoEExitNode).where(PPPoEExitNode.state == "idle")).all()
    # Pick a random sample of 15 proxies to test anonymity
    sample_nodes = random.sample(nodes, min(15, len(nodes)))
    proxies = [{"slot": n.ppp_slot, "expected_ip": n.public_ip, "proxy": n.proxy_url.replace("socks5://", "socks5h://")} for n in sample_nodes if n.proxy_url]

def check_anonymity(proxy_info):
    slot = proxy_info["slot"]
    expected_ip = proxy_info["expected_ip"]
    proxy = proxy_info["proxy"]
    
    result = {"slot": slot, "expected_ip": expected_ip, "actual_ip": None, "asn": None, "country": None, "status": "FAILED"}
    
    try:
        # Check IP info
        cmd = ["curl", "-4", "-s", "-x", proxy, "--max-time", "10", "https://ipinfo.io/json"]
        out = subprocess.check_output(cmd, text=True)
        data = json.loads(out)
        
        result["actual_ip"] = data.get("ip")
        result["asn"] = data.get("org")
        result["country"] = data.get("country")
        
        if result["actual_ip"] == expected_ip and "Airtel" in str(result["asn"]):
            result["status"] = "PASSED"
        else:
            result["status"] = "WARNING (Mismatch or Non-Airtel ASN)"
            
    except Exception as e:
        result["status"] = f"ERROR: {str(e)}"
        
    return result

print(f"Running Anonymity & ASN validation on {len(proxies)} sampled proxies...")
with concurrent.futures.ThreadPoolExecutor(max_workers=15) as executor:
    for res in executor.map(check_anonymity, proxies):
        print(f"Slot {res['slot']} | Expected: {res['expected_ip']} | Detected: {res['actual_ip']} | ASN: {res['asn']} | Status: {res['status']}")

