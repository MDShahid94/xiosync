from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
OPS = ROOT / "ops"


def test_backup_script_exists():
    assert (OPS / "backup.sh").is_file()


def test_restore_script_exists():
    assert (OPS / "restore.sh").is_file()


def test_schedule_backup_script_exists():
    assert (OPS / "schedule-backup.sh").is_file()


def test_dr_runbook_exists():
    assert (ROOT / "docs" / "ops" / "DR-RUNBOOK.md").is_file()


def test_dr_runbook_has_drill_record():
    content = (ROOT / "docs" / "ops" / "DR-RUNBOOK.md").read_text()
    rows = [line for line in content.splitlines() if line.startswith("|")]
    assert any("2026-08-26" in row for row in rows)
    assert len(rows) >= 3


def test_scripts_are_executable():
    scripts = list(OPS.glob("*.sh"))
    assert scripts
    assert all(path.stat().st_mode & 0o111 for path in scripts)
