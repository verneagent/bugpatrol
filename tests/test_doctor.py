from __future__ import annotations

import unittest
from pathlib import Path

from bugpatrol.config import load_project_config
from bugpatrol.doctor import run_doctor
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

        checks = run_doctor(
            config=config,
            github=FakeGithub(),  # type: ignore[arg-type]
            issue_fields=FakeIssueFields(),  # type: ignore[arg-type]
        )

        self.assertTrue(all(check.ok for check in checks))
        self.assertEqual([check.name for check in checks], ["config", "github_repo", "issue_types", "issue_fields"])


if __name__ == "__main__":
    unittest.main()
