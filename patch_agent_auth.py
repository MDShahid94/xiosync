import re

with open("colab/xiorun_agent.py", "r") as f:
    code = f.read()

auth_block = """from fastapi.security import APIKeyHeader
from fastapi import Security

api_key_header = APIKeyHeader(name="X-Worker-Secret", auto_error=False)

def verify_worker_secret(api_key: str = Security(api_key_header)):
    if not XIOSYNC_TOKEN:
        return api_key
    if api_key != XIOSYNC_TOKEN:
        raise HTTPException(status_code=401, detail="Unauthorized: Invalid X-Worker-Secret")
    return api_key

from fastapi import Depends
app = FastAPI(title="XIOSYNC Colab Agent", dependencies=[Depends(verify_worker_secret)])
"""

code = re.sub(
    r"app = FastAPI\(title=\"XIOSYNC Colab Agent\"\)",
    auth_block,
    code
)

with open("colab/xiorun_agent.py", "w") as f:
    f.write(code)
