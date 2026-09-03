"""Bootstrap XIOBR Workflow Templates."""

from sqlalchemy.orm import Session
from xiosync.domain.context import OrgContext
from xiosync.subsystems.xiogrid.services.workflow_templates import WorkflowTemplateService

def register_xiobr_templates(session: Session, context: OrgContext) -> None:
    svc = WorkflowTemplateService(session)
    
    # 1. _test-runner.mjs
    svc.register_template(
        context,
        name="Test Runner",
        category="maintenance",
        steps=[{"id": "run_test_runner_script", "script": "_test-runner.mjs"}],
        config={"retry": 3}
    )
    # 2. agy-install.mjs
    svc.register_template(
        context,
        name="Agy Install",
        category="infrastructure",
        steps=[{"id": "run_agy_install_script", "script": "agy-install.mjs"}],
        config={"retry": 3}
    )
    # 3. disconnect-all.mjs
    svc.register_template(
        context,
        name="Disconnect All",
        category="maintenance",
        steps=[{"id": "run_disconnect_all_script", "script": "disconnect-all.mjs"}],
        config={"retry": 3}
    )
    # 4. google-session-refresh.mjs
    svc.register_template(
        context,
        name="Google Session Refresh",
        category="auth_flow",
        steps=[{"id": "run_google_session_refresh_script", "script": "google-session-refresh.mjs"}],
        config={"retry": 3}
    )
    # 5. google-signin.mjs
    svc.register_template(
        context,
        name="Google Signin",
        category="auth_flow",
        steps=[{"id": "run_google_signin_script", "script": "google-signin.mjs"}],
        config={"retry": 3}
    )
    # 6. self-spawn.mjs
    svc.register_template(
        context,
        name="Self Spawn",
        category="infrastructure",
        steps=[{"id": "run_self_spawn_script", "script": "self-spawn.mjs"}],
        config={"retry": 3}
    )
    # 7. tailscale-auth.mjs
    svc.register_template(
        context,
        name="Tailscale Auth",
        category="auth_flow",
        steps=[{"id": "run_tailscale_auth_script", "script": "tailscale-auth.mjs"}],
        config={"retry": 3}
    )
    # 8. tailscale-signin.mjs
    svc.register_template(
        context,
        name="Tailscale Signin",
        category="auth_flow",
        steps=[{"id": "run_tailscale_signin_script", "script": "tailscale-signin.mjs"}],
        config={"retry": 3}
    )
    # 9. tailscale-ssh-auth.mjs
    svc.register_template(
        context,
        name="Tailscale SSH Auth",
        category="auth_flow",
        steps=[{"id": "run_tailscale_ssh_auth_script", "script": "tailscale-ssh-auth.mjs"}],
        config={"retry": 3}
    )
    # 10. v0-export-session.mjs
    svc.register_template(
        context,
        name="V0 Export Session",
        category="browser_automation",
        steps=[{"id": "run_v0_export_session_script", "script": "v0-export-session.mjs"}],
        config={"retry": 3}
    )
    # 11. v0-signin-with-google.mjs
    svc.register_template(
        context,
        name="V0 Signin With Google",
        category="auth_flow",
        steps=[{"id": "run_v0_signin_with_google_script", "script": "v0-signin-with-google.mjs"}],
        config={"retry": 3}
    )
    # 12. v0-signin.mjs
    svc.register_template(
        context,
        name="V0 Signin",
        category="auth_flow",
        steps=[{"id": "run_v0_signin_script", "script": "v0-signin.mjs"}],
        config={"retry": 3}
    )
