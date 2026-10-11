with open('xiosync/subsystems/xioflow/api/events.py', 'r') as f:
    code = f.read()

old_sql = """            INSERT INTO xioflow_tasks
              (id, run_id, node_intent, state, attempt_count, claimed_at)
            VALUES (:id, :run_id, :intent, 'CLAIMED', 1, now())
        "),
            {"id": task_id, "run_id": str(row.id), "intent": row.template_name or "dag_root"},"""

new_sql = """            INSERT INTO xioflow_tasks
              (id, run_id, organization_id, node_intent, state, attempt_count, claimed_at)
            VALUES (:id, :run_id, :org_id, :intent, 'CLAIMED', 1, now())
        "),
            {
                "id": task_id, 
                "run_id": str(row.id), 
                "org_id": str(row.organization_id),
                "intent": row.template_name or "dag_root"
            },"""

if old_sql in code:
    code = code.replace(old_sql, new_sql)
    with open('xiosync/subsystems/xioflow/api/events.py', 'w') as f:
        f.write(code)
    print("Patched successfully")
else:
    print("Old SQL not found!")
