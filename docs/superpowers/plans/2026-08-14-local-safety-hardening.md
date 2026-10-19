# Local Safety Hardening Implementation Plan

> **For agentic workers:** Execute inline with test-driven-development. Preserve existing user work; do not commit automatically.

**Goal:** Confine destructive and import operations to their intended roots and bind the local UI to loopback.

**Architecture:** Keep existing FastAPI routes and installer behavior. Validate resolved filesystem paths before writes/deletes; reject nonportable archive names and archive links. Preview and apply must share import target rules.

**Tech Stack:** Python >=3.11, FastAPI, standard-library unittest and pathlib.

**Spec:** User-approved scope in conversation: uninstall/archive/import security fixes, loopback binding, regression tests, development checks, documentation corrections. No module moves, new dependencies, or feature redesign.

## Global Constraints

- Preserve existing script-layout uninstall support in app/installer.py and app/main.py.
- Test with TemporaryDirectory; never uninstall or change real user tools/configuration.
- Watch security regressions fail before production changes.
- No authentication feature added; document local-only operation and residual risk.

## Review Focus

- Windows drive, UNC and backslash paths must be rejected on every host.
- Existing symlinks must not redirect writes/deletes outside allowed roots.
- Invalid archive uploads must not destroy an existing installation.
- Valid exports, JSON merges, backups, patches and script uninstalls must still work.
- Preview skip counts must agree with apply behavior, including missing packages.

## Tasks

### Task 1: Pin Security Boundaries

Files: tests/__init__.py, tests/test_main.py, tests/test_installer.py, tests/test_bundle.py.

- [x] Write unittest regressions for unsafe paths, unsafe archive members, uninstall validation and CLI binding; include safe workflows.
- [x] Run `.venv/bin/python -m unittest discover -s tests -v`; confirm failures correspond to demonstrated bugs.

### Task 2: Fix Root Causes

Files: app/main.py, app/installer.py, app/bundle.py, app/paths.py.

- [x] Validate every explicit uninstall prefix against detected installs before selecting a deletion branch; confine whole-directory removal to the requested agent's direct archive installation.
- [x] Resolve script prefix paths, reject root/home and escaping package/bin directories, revalidate before deletion, report filesystem failures.
- [x] Preflight archive member paths and types; reject links/special files; stage uploads with a server-controlled filename before replacing an installation.
- [x] Apply consistent home/package containment to import preview and apply, including legacy absolute targets.
- [x] Bind CLI to 127.0.0.1 and preserve --port.
- [x] Rerun full suite until green.

### Task 3: Document and Verify

Files: Makefile, README.md.

- [x] Add dependency-free development test/check commands.
- [x] Correct registered-tool count, network/password/WebDAV claims and document import restrictions.
- [x] Run suite, compileall, import/registry smoke check and git diff --check.
- [x] Review the full diff and start a loopback development server on an available port for manual use.

## Execution Notes

- Delegation unavailable: provider reports `No API key found for kimi-coding.` Execute inline.
- Work in the existing user-approved checkout; do not move or discard pre-existing edits.
- Task 1 RED: 32 tests, 50 failing subtests, zero errors after correcting a tar directory fixture permission. Failures reproduce the intended safety defects; normal export/JSON merge, text remapping and script cleanup already pass.
- Ruling: Add app/paths.py with a shared relative_target(root, relative, allow_root=False) validator; archive and bundle paths need identical cross-platform and resolved-symlink checks. This avoids divergent validation.
- Ruling: Stage archives before replacing existing installations; otherwise invalid archives destroy valid programs. Use server-generated paths, never uploaded filenames.
