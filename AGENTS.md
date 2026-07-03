# bugpatrol

Treat `bugpatrol` as a product, not a one-off script.

## Working Practice

Work one closed loop at a time:

1. Define the smallest user-visible workflow slice.
2. Implement the deterministic product logic first.
3. Verify the slice yourself before reporting it as done.
4. Turn the verification into reusable tests.
5. Keep both unit tests and e2e tests for each meaningful workflow.
6. Clearly distinguish fake/local e2e from live sandbox e2e.

Do not claim a live integration is verified unless the real external system was
called and the result was read back.

## Test Expectations

- Unit tests cover pure logic, validation, parsing, command construction, and
  client behavior with mocked external commands.
- Local e2e tests use reusable fixtures and fake clients to exercise complete
  workflows without network access.
- Live e2e tests must be opt-in via environment variables, run only against the
  sandbox repo/group, and clean up test issues or messages where the platform
  allows it.

## Product Boundaries

- Intake records what the reporter said. It does not triage.
- Triage is a separate workflow.
- Fixing is out of scope until intake and triage are stable.
- GitHub issues and fields are the durable workflow surface.
- Intake metadata is the worker ownership boundary. Triage and notification
  workers must only write issues that contain `BUGPATROL_INTAKE_META`.
- Other hidden metadata is for idempotency and backlinks.
