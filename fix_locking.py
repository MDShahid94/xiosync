import re

with open("xiosync/subsystems/xiogrid/services/pppoe_nodes.py") as f:
    code = f.read()

# 1. Add with_for_update(skip_locked=True) to _pick_idle_node
replacement_pick = """            .limit(1)
            .with_for_update(skip_locked=True, of=PPPoEExitNode)
        )"""

code = re.sub(r"            \.limit\(1\)\n        \)", replacement_pick, code, flags=re.DOTALL)

# 2. Add with_for_update() to _get_node so assign_to_worker locks it
replacement_get = """    def _get_node(self, host_id: uuid.UUID, slot: int) -> PPPoEExitNode:
        node = self._db.scalar(
            select(PPPoEExitNode).where(
                PPPoEExitNode.host_id == host_id,
                PPPoEExitNode.ppp_slot == slot,
            ).with_for_update()
        )
        if not node:
            raise ValueError(f"Slot {slot} on host {host_id} not in DB")
        return node"""

code = re.sub(
    r"    def _get_node\(self, host_id: uuid\.UUID, slot: int\) -> PPPoEExitNode:\n        node = self\._db\.scalar\(\n            select\(PPPoEExitNode\)\.where\(\n                PPPoEExitNode\.host_id == host_id,\n                PPPoEExitNode\.ppp_slot == slot,\n            \)\n        \)\n        if not node:\n            raise ValueError\(f\"Slot \{slot\} on host \{host_id\} not in DB\"\)\n        return node",
    replacement_get,
    code,
    flags=re.DOTALL,
)

with open("xiosync/subsystems/xiogrid/services/pppoe_nodes.py", "w") as f:
    f.write(code)
