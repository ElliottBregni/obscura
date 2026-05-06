<!--
  Thanks for contributing. Fill out everything that applies. Delete the
  sections that don't. Reviewers will block on missing checklist items.
-->

## What

<!-- One paragraph: what does this change do? -->

## Why

<!-- One paragraph: what user-visible problem is this solving, or what
     internal quality is being raised? Include a bug id / issue link. -->

## How to test

<!-- Manual repro steps, expected output, screenshots / video for UI. -->

---

## Checklist

**Always:**
- [ ] Title prefixed: `[ext]`, `[ext][host]`, `[ext][panel]`,
      `[ext][proto]`, or `[core]` for obscura core changes.
- [ ] If this PR should cut a new release, **at least one commit
      message** starts with `major:`, `minor:`, or `patch:` (or the
      `[major]` / `(minor)` wrapped form, or a `feat!:` / `fix!:`
      style breaking-change `!`). The CI **Semver check** job will
      tell you what (if any) bump it computed. PRs without any of
      those markers merge cleanly but do **not** cut a release.
- [ ] Commits are undercover-clean — no AI attribution, first person.
- [ ] `ruff check` + `ruff format --check` + `pyright` pass locally.
- [ ] Net diff under ~500 LOC *or* split into multiple PRs.

**If you touched `packages/browser-extension/`:**
- [ ] `npm run check` + `npm test` pass locally.
- [ ] If you added a **wire frame**: updated the table in
      `packages/browser-extension/ARCHITECTURE.md` *and* the protocol
      docstring at the top of `obscura_native_host.py`.
- [ ] If you added a **slash command** or **browser tool**: test added
      under `tests/` or `tests/browser_extension/`.
- [ ] If you changed the **storage schema**: `STORAGE_VERSION` bumped
      *and* `migrateStorage()` updated in the same commit.
- [ ] If you changed the **launcher or host env**: tested
      `obscura-browser reload` + Chrome reload loop.

**If you touched `obscura/`:**
- [ ] Public API change? → existing tests updated, new tests added.
- [ ] Touched `obscura/cli/commands.py:COMMANDS` or widget
      signatures? → smoke-tested that the browser panel still behaves
      (reload host + send / command).

**Review routing** (reviewers: confirm before approving):
- [ ] 2 approvals required — because this changes the **wire
      protocol**, **session lifecycle**, **storage schema**, or a
      **monkey-patch installer** in the native host.
- [ ] 1 approval required — everything else.

## Notes for reviewers

<!-- Anything unusual: perf trade-offs, follow-up tickets, known flaky
     tests, design alternatives rejected, etc. Delete if empty. -->
