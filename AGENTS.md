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

## Lark Notifications

- To @-mention a person in ANY message or reply sent to Lark, always emit a real
  at-tag `<at user_id="OPEN_ID">name</at>` (open_id from
  `config.lark.user_open_ids`). `build_post_content` in `lark.py` renders these
  into real Lark mentions that actually ping the person.
- NEVER write plain-text `@name` / `@login` for a mention — it looks right but
  does not notify anyone. This bug has recurred (triage summaries, `/assign`
  reply); assert on the `<at ...>` tag in tests, not on `@name`.
- For links in Lark messages, always emit a masked markdown link with a short
  label: `[#3996](url)` / `[abc123def456](url)`, never a bare full URL.
  `reply_to_message` renders `[text](url)` as a clickable rich-text link; a bare
  URL shows the whole ugly URL and is not what we want. This has recurred for PR
  links (fix PR notifications, reconcile). Issue/PR labels are `#<number>`,
  commits are the 12-char short SHA. Assert on `[#N](url)` in tests, not the URL.

## Product Boundaries

- Intake records what the reporter said. It does not triage.
- Triage is a separate workflow.
- Fixing is out of scope until intake and triage are stable.
- GitHub issues and fields are the durable workflow surface.
- Intake metadata is the worker ownership boundary. Triage and notification
  workers must only write issues that contain `BUGPATROL_INTAKE_META`.
- Other hidden metadata is for idempotency and backlinks.
