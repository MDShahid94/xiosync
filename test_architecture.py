import multiprocessing
import time

import httpx
import uvicorn
from fastapi import FastAPI

app = FastAPI()


@app.get("/test")
def test():
    from xiosync.subsystems.xiorun.runtime_pool import get_runtime_pool

    pool = get_runtime_pool()
    return {"active_runs": pool._active_runs}


def run_api():
    uvicorn.run(app, host="127.0.0.1", port=8001, log_level="error")


def run_worker():
    from xiosync.subsystems.xiorun.runtime_pool import get_runtime_pool

    pool = get_runtime_pool()
    pool.register_active_run("test-session", "test-run", "script")
    print("Worker registered active run. Sleeping 5s...")
    time.sleep(5)
    print("Worker done.")


if __name__ == "__main__":
    p_api = multiprocessing.Process(target=run_api)
    p_api.start()

    time.sleep(2)  # wait for API to start

    p_worker = multiprocessing.Process(target=run_worker)
    p_worker.start()

    time.sleep(1)  # wait for worker to register

    res = httpx.get("http://127.0.0.1:8001/test").json()
    print("API sees active_runs:", res)

    p_api.terminate()
    p_worker.terminate()
