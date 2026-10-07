import re

with open("colab/xiorun_agent.py") as f:
    code = f.read()

replacement_func = """    # Deterministic path convention
    _node_slug = NODE_NAME.replace("-", "_")
    _re_prof = __import__("re").compile(r"[^a-zA-Z0-9]")
    if req.identity_id:
        _id16 = req.identity_id.replace("-", "")[:16]
        _profile_dir = f"/tmp/xiorun_profiles/PRFL_{_id16}__{_node_slug}"
    else:
        _email_slug = _re_prof.sub("_", req.email.split("@")[0].lower())[:32]
        _profile_dir = f"/tmp/xiorun_profiles/PRFL_{_email_slug}__{_node_slug}"
    
    os.makedirs(_profile_dir, exist_ok=True)
    logger.info(f"run-uc-login-start: using profile_dir={_profile_dir}")"""

# Regex replacing the old logic in run_uc_login_start
code = re.sub(
    r'    # Deterministic path convention\n.*?    logger\.info\(f"run-uc-login-start: using profile_dir=\{_profile_dir\}"\)',
    replacement_func,
    code,
    flags=re.DOTALL,
)

with open("colab/xiorun_agent.py", "w") as f:
    f.write(code)
