# Life runtime

The production path is OneBot -> RoleHost -> one DSH role Agent -> typed life commits -> Delivery. Python owns durable state, identity, scope, execution eligibility, recovery and platform receipts. DSH owns understanding, action selection and natural expression. There is no Brain/shadow switch or parallel Python cognition loop.

- `role_host.py`, `role_agent.py`, `agent_session.py`: transport, the role and persistent inbox reconciliation.
- `expression.py`, `message_layout.py`, `delivery.py`: frozen expression plans, stable reply slots, 2-5 second chat cadence, whole text at 300 characters with a 4 second delay, and confirmed segment experiences.
- `appraisals.py`, `state.py`, `drives.py`: semantic stimulus accounting and revisions, shared affect and independent continuous drives. Context references do not consume stimuli again.
- `persona.py`, `self_change.py`: one partitioned persona, a derived public view and verified edits. Private sections never enter the public view.
- `tasks.py`, `role_agent.py`: intentions, waiting for real answers, cancellation and delegated work. The principal owns public account expression.
- `release.py`, `release_bundle.py`, `release_origin.py`, `release_migrations.py`, `supervisor.py`: independent candidate checks, multi-file/core/new-module updates, additive migration and restart recovery. Publication eligibility is checked at the actual source switch. The minimal bootstrap and independent checks remain external.

Use the root README and `config.example.toml` for current configuration. The three QQ owner-private commands are `/状态`, `/重启` and `/清空记忆`. Normal requests and task controls use natural language.

Verification: `.venv\Scripts\python.exe -B -X utf8 audit/run_offline_suite.py`. The independent review's cases are regression tests in `tests/test_review_regressions.py`; publication and compatible database migration checks are in `tests/test_release_bundle.py`. `audit/run_core_publish_smoke.py` verifies real controller re-execution with a synthetic worker and no QQ messages. Real model/OneBot tests distinguish fake-platform and actual-platform receipts.

Native candidate execution is ordinary local execution, as selected by the user. It is not an OS sandbox. Compatible migrations preserve existing rows; code rollback does not restore old life data. Arbitrary voice cloning, undeclared third-party plugin side effects, unavailable account logins and physical erasure of all external caches are not implied by these checks.
