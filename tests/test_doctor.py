from __future__ import annotations

import unittest
import subprocess
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from bugpatrol.config import load_project_config
from bugpatrol.doctor import _triage_agent_auth_command, run_doctor
from bugpatrol.github_fields import IssueField


class FakeGithub:
    def get_repository(self, *, repo: str) -> dict[str, object]:
        return {"full_name": repo, "private": True}

    def list_issue_types(self, *, repo: str) -> tuple[str, ...]:
        return ("Bug", "Feature", "Task")


class FakeIssueFields:
    def list_org_fields(self, *, org: str) -> dict[str, IssueField]:
        from bugpatrol.fields import default_field_specs

        return {
            name: IssueField(id=i, name=name, data_type="single_select", options=spec.values)
            for i, (name, spec) in enumerate(default_field_specs().items(), start=1)
        }


class DoctorTest(unittest.TestCase):
    def test_run_doctor_reports_all_checks_ok(self) -> None:
        config = load_project_config(Path("projects/todo-sandbox.toml"))

        with patch("bugpatrol.doctor.shutil.which", return_value="/usr/bin/tool"):
            with patch("bugpatrol.doctor.subprocess.run") as run:
                run.return_value = subprocess.CompletedProcess(
                    ["codex", "login", "status"],
                    0,
                    "Logged in using ChatGPT\n",
                    "",
                )
                checks = run_doctor(
                    config=config,
                    github=FakeGithub(),  # type: ignore[arg-type]
                    issue_fields=FakeIssueFields(),  # type: ignore[arg-type]
                )

        self.assertTrue(all(check.ok for check in checks))
        self.assertEqual(
            [check.name for check in checks],
            [
                "config",
                "github_repo",
                "issue_types",
                "issue_fields",
                "asset_repo",
                "media_vision_command",
                "ffmpeg",
                "triage_agent",
                "triage_agent_auth",
            ],
        )
        run.assert_called_once_with(
            ["codex", "login", "status"],
            capture_output=True,
            text=True,
            check=False,
            timeout=30,
        )

    def test_triage_agent_auth_command_matches_provider(self) -> None:
        config = load_project_config(Path("projects/todo-sandbox.toml"))
        claude = replace(config, triage_agent=replace(config.triage_agent, provider="claude"))

        self.assertEqual(_triage_agent_auth_command(config), ("codex", "login", "status"))
        self.assertEqual(_triage_agent_auth_command(claude), ("claude", "auth", "status"))

    def test_doctor_reports_agent_auth_failure(self) -> None:
        config = load_project_config(Path("projects/todo-sandbox.toml"))

        with patch("bugpatrol.doctor.shutil.which", return_value="/usr/bin/tool"):
            with patch("bugpatrol.doctor.subprocess.run") as run:
                run.return_value = subprocess.CompletedProcess(
                    ["codex", "login", "status"],
                    1,
                    "",
                    "not logged in",
                )
                checks = run_doctor(
                    config=config,
                    github=FakeGithub(),  # type: ignore[arg-type]
                    issue_fields=FakeIssueFields(),  # type: ignore[arg-type]
                )

        auth = next(check for check in checks if check.name == "triage_agent_auth")
        self.assertFalse(auth.ok)
        self.assertIn("not logged in", auth.detail)


if __name__ == "__main__":
    unittest.main()
