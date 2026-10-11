import asyncio
from xiosync.platform.config import load_config
import os
os.environ["XIOSYNC_ENVIRONMENT"] = "development"
os.environ["XIOSYNC_INTERNAL_SECRET"] = "xiosync-internal-2026-karma"

from xiosync.platform.engine_ref import set_engine
from xiosync.persistence.database import create_database_engine
from sqlalchemy.orm import sessionmaker
from xiosync.subsystems.xioflow.api.runs import DispatchRunRequest

# Let's see if there is a function we can call
from xiosync.subsystems.xioflow.api.runs import dispatch_run

print("Dependencies loaded")
