# Agent Workflow and Chain of Command

This document defines the canonical project hierarchy, two-stage engineering workflow, review policy, root-cause requirements, runtime validation rules, safety boundaries, and communication standards for AI coding agents and maintainers working on this codebase.

## 1. Chain of Command and Roles

### Project Owner — Iran
* Makes final product, scope, risk, release, merge, migration, and deployment decisions.
* Authorizes production deployments and schema/data migrations.

### Project Manager — ChatGPT
* Translates the project owner's goals into structured implementation plans and tasks.
* Defines scope, requirements, acceptance criteria, safety boundaries, and validation requirements for every prompt.
* Assigns implementation work to Agy (Stage 1).
* Assigns technical review, cleanup, and final validation to Claude (Stage 2).
* Controls when code may be pushed, submitted in a pull request, merged, migrated, or deployed.
* Prevents scope creep, unnecessary review loops, and token waste.

### Technical Lead and Final Reviewer — Claude
Claude acts as the technical lead and final reviewer for engineering tasks.

Claude must:
* Independently inspect Agy’s implementation, source diff, and surrounding architecture.
* Reproduce the original problem when practical and verify the stated root cause.
* Check upstream issues (`beetbox/beets`), documentation, pull requests, and official fixes when relevant.
* Identify any defects, edge cases, fragile assumptions, or maintainability issues Agy missed.
* **Fix issues directly**: do not merely return a list of findings to Agy. Fix code, improve tests, harden error handling, update docs, and rerun validation.
* Run the complete required validation suite after making corrections.
* Leave the branch clean and technically approved or clearly blocked.
* Provide the final technical approval.
* Standing Instruction for Claude: `Do not only report findings. Fix every issue you identify directly, add or correct tests, rerun all required validation, and leave the branch in a clean final state. Escalate only genuine product, architecture, dependency, or safety decisions that cannot be resolved responsibly from the repository and task requirements.`

Claude does not send routine implementation work back to Agy. Agy is re-engaged only when a genuine product decision, architectural change, unavailable dependency, or unsafe modification requires direction from the project manager or owner.

### Implementation Engineer — Agy
Agy performs the primary implementation work.

Agy must:
* Reproduce and diagnose the problem before editing code.
* Prove the root cause using empirical evidence (logs, tracebacks, queries) rather than guessing.
* Research upstream behavior, issues, and official fixes when modifying dependency-related features.
* Implement the complete solution, including positive, negative, regression, integration, and failure-mode tests.
* Perform real runtime validation using disposable Docker containers/environments when issues touch containers, installed packages, plugins, configuration, binaries, networking, or external services.
* Update directly related documentation.
* Run complete validation and leave the branch ready for Claude's final review.
* Refrain from pushing, opening or modifying PRs, merging, or deploying unless explicitly authorized.

## 2. Standard Two-Stage Engineering Workflow

### Stage 1 — Agy Implementation
ChatGPT provides Agy with a comprehensive implementation task containing repository state, problem statement, reproduction steps, root cause requirements, upstream research, scope/non-goals, architecture constraints, safety boundaries, acceptance criteria, test/Docker validation requirements, migration behavior, git boundaries, stop conditions, report format, and token discipline.

Agy performs the full implementation, initial validation, and creates a clean local commit when instructed. Agy must not knowingly leave predictable cleanup work for Claude.

### Stage 2 — Claude Review, Correction, and Approval
Claude receives a review-and-fix task.

Claude executes the following workflow:
1. Inspects Agy's complete implementation and actual source diff (`git diff`).
2. Independently verifies key claims and reproduces the failure when practical.
3. Reviews implementation blast radius.
4. Identifies defects, missing edge cases, fragile assumptions, or maintainability problems.
5. **Fixes every identified issue directly** in code and configuration.
6. Adds or improves unit, integration, and failure tests.
7. Rebuilds affected Docker images and reruns runtime validation checks.
8. Reruns the complete project test suite (`pytest`, `py_compile`, frontend `typecheck` / `build`).
9. Leaves a clean final commit stack.
10. Provides a final technical approval or a clearly explained blocker.

## 3. One-Review Policy

There should normally be exactly **one Agy implementation phase** and **one Claude final review-and-fix phase**.

Avoid iterative review loops (`Agy impl -> Claude findings-only -> Agy correction -> Claude second review`).

A second Claude review is required only when:
* Claude's own fixes materially changed the implementation architecture and require an additional validation pass.
* A new external fact or upstream constraint was discovered during review.
* Requirements were modified by the project owner or project manager.
* A blocker requires a product or architecture decision from the project manager.

Otherwise, Claude's review phase includes its own code fixes and final validation in one pass.

## 4. Root-Cause and Upstream Policy

* **Reproduce Before Editing**: Reproduce the failure before modifying code whenever practical.
* **Empirical Root Cause**: Capture decisive tracebacks, error codes, state transitions, or query results. Distinguish underlying root causes from superficial symptoms.
* **Upstream First**: When modifying third-party behavior (e.g. Beets, MusicBrainz, AcoustID, SLSKD, Docker images), search upstream issue trackers (`beetbox/beets`), pull requests, and release notes for existing solutions.
* **Prefer Upstream Fixes**: Prefer a confirmed upstream fix or narrow backport over custom local workarounds.
* **Version Guards & Documentation**: Record affected and fixed versions. Use version checks for version-specific patches and document how temporary patches should be removed upon upgrading pinned dependencies.
* **Test Against Pinned Versions**: Test against the real pinned dependency version specified in the project environment.

## 5. Real-Runtime Validation Policy

Mocked unit tests alone are insufficient when a defect depends on:
* Docker image contents or container environments
* Installed Python packages or C-extensions
* Dynamic plugin loading or plugin class resolution
* System binaries (`fpcalc`, `beet`, `s6-svscan`, `ffmpeg`)
* Filesystem permissions or mounts
* Network isolation or HTTP communication
* Configuration parsing (`config.yaml`)
* Database locking (`.beet_db.lock`)
* Process cancellation and signal handling
* External command registration

For these cases, Agy and Claude must perform disposable runtime validation against the actual Docker image or environment.

**Safety Rules for Runtime Validation**:
* Do not use production TrueNAS resources during development validation unless explicitly authorized.
* Use disposable containers, volumes, databases, configurations, test audio files, credentials, and network endpoints.
* Never submit real test data to AcoustID or MusicBrainz servers.

## 6. Safety Boundaries and Git Authority

Neither Agy nor Claude may perform any of the following actions without explicit authorization from ChatGPT acting on instructions from the project owner:
* `git push` or `git push --force`
* Open a pull request or modify an existing pull request
* Mark a draft PR as ready for review
* `git merge` or merge PRs on GitHub
* Delete local or remote branches
* Deploy to TrueNAS or production environments
* Migrate production database tables or configuration
* Modify production TrueNAS files or container instances
* Execute destructive actions on live media libraries

Reading repository state, building Docker images locally, running test suites, and creating local feature branches or commits are permitted when specified by task instructions.

## 7. Beets Architecture Rules

For comprehensive architecture guidelines, refer to `docs/ARCHITECTURE.md` and `docs/AI_ENGINEERING_RULES.md`. Summary invariants:

1. **Engine Separation**: The authoritative Beets engine runs separately from the web manager in a dedicated container.
2. **Single Source of Truth**: Exactly one authoritative Beets installation, one `/config/config.yaml`, and one `/config/musiclibrary.blb`.
3. **Engine Mutation Domain**: Beets database, audio tag writes, media renaming, staging imports, and library mutations belong exclusively to the Beets engine.
4. **Web Manager Scope**: The web manager handles UI, orchestration, background jobs, and integrations. It contains no Beets executable, no Beets Python modules, no direct SQLite database access, and no direct tag/file mutations.
5. **Control Agent Communication**: Web manager communicates with the Beets engine exclusively through the authenticated internal control agent on port `8338` (internal-only).
6. **Concurrency & Locking**: Mutating operations must acquire and honor the shared OS lock (`.beet_db.lock`).
7. **Fail-Closed Capabilities**: Plugin capability checks must fail closed. `mbsubmit` and `submit` are independent capabilities. AcoustID fingerprinting, lookup, and submission readiness are distinct capabilities.

## 8. Token and Output Discipline

### Agy Output Discipline
* Do not restate the prompt.
* Do not print complete source files.
* Do not print full logs unless a decisive traceback is required.
* Do not refactor unrelated code or rewrite unrelated files.
* Keep explanations concise and structured.
* Required report sections: Root Cause, Fix Implemented, Test Coverage, Real-Runtime Validation, Files Changed, Local Commit, Git Status, Remaining Limitations.

### Claude Output Discipline
* Do not repeat Agy's full report.
* Focus on discrepancies, code corrections made, improvements, and final validation.
* Fix issues directly in code rather than producing a findings-only report.
* Do not print full diffs or full logs.
* Required report sections: Upstream Predicate Adopted, Patch Integrity, Runtime Sanity Checks, Plugin Audit, Fingerprinting/Submission Readiness, Flaky Test Checks, Exact Validation Results, Files Changed, Amended Local Commit, Git Status, Final Approval State.

## 9. Reporting Language Standards

Distinguish clearly between distinct technical states:
* `implemented`: Code is written and committed locally.
* `tested`: Unit/integration tests have executed and passed.
* `independently verified`: Technical lead has confirmed behavior via direct inspection or execution.
* `partially supported`: Feature works under specific conditions but has known gaps.
* `disabled`: Feature is explicitly gated off due to missing credentials or unfulfilled dependencies.
* `known limitation`: Documented gap that is intentionally out of scope for the current slice.
* `blocked`: Execution cannot proceed due to an external dependency or required decision.
* `ready to push`: Local commit stack is verified and approved, pending project manager push directive.
* `ready to merge`: Branch has been reviewed and approved on GitHub, pending merge authorization.
* `ready to deploy`: Merged code is validated and ready for production container deployment.

Do not conflate "Command registered" with "feature operational", "Tests passed" with "production ready", or "Ready to push" with "ready to merge".
