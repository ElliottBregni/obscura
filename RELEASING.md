# Releasing Obscura

Releases are **fully automated** from `main`. Authors write semver-aware
commit messages; CI does the rest.

## Flow

```
PR → squash-merge to main
        │
        ▼
auto-release.yml          (.github/workflows/auto-release.yml)
   - reads commits since last vX.Y.Z tag
   - picks highest of major / minor / patch
   - bumps pyproject.toml + browser-extension package.json + manifest.json
   - commits "chore(release): vX.Y.Z (kind) [skip ci]"
   - pushes a vX.Y.Z tag
        │
        ▼
release.yml               (.github/workflows/release.yml)
   - runs the full CI gate
   - cuts the GitHub Release with a generated changelog
```

If no commit on `main` since the last tag carries a semver marker, no
release happens — the workflow exits with a `notice` and that's it.

## Commit-message convention

Markers go on the **first line** of the commit. Case-insensitive.

| Marker forms                              | Bump   | Resulting jump |
| ----------------------------------------- | ------ | -------------- |
| `major: ...`, `[major] ...`, `(major) ...`| major  | 1.2.3 → 2.0.0  |
| `minor: ...`, `[minor] ...`, `(minor) ...`| minor  | 1.2.3 → 1.3.0  |
| `patch: ...`, `[patch] ...`, `(patch) ...`| patch  | 1.2.3 → 1.2.4  |
| `feat!: ...`, `fix(api)!: ...`            | major  | (conventional commits `!` suffix) |
| anything else                             | none   | no release     |

The keyword has to be a deliberate prefix marker. English uses like
`monkey-patch the loop` or `stage minor refactor` are intentionally
**not** detected.

If multiple markers appear across the commits being released, the
**highest** wins (major beats minor beats patch).

Use squash-merge so the marker on the PR's title/squash-message is what
ends up on `main`.

## One-time GitHub setup

Branch protection plus auto-bumping is a chicken-and-egg: the workflow
has to push a commit + tag to a protected branch. Configure once:

1. **Settings → Actions → General → Workflow permissions**
   → "Read and write permissions" enabled.

2. **Settings → Branches → Branch protection rule for `main`**
   - Require pull request before merging ✓
   - Require status checks to pass ✓
     - Required: `CI / CI success`
   - Require linear history ✓ (recommended; works with squash-merge)
   - Do **not** check "Include administrators" — leave it off so the
     bot's release commit can land. Or, equivalently:
   - **"Allow specified actors to bypass required pull requests"** →
     add `github-actions[bot]`.
   - Restrict who can push: leave open to everyone via PR; the rule
     already forbids direct pushes since "Require PR" is on.

3. **Settings → General → Pull Requests**
   - Allow squash merging ✓ (preferred — keeps semver markers tidy)
   - Disable merge commits and rebase merging if you want one canonical
     style.

After that, `git push origin main` from a laptop is rejected; the only
path to `main` is via PR; merging a PR with a `major:` / `minor:` /
`patch:` marker triggers a release automatically.

## Manual override

If you need to run a release out-of-band:

```bash
python scripts/bump_version.py            # bumps based on commits since last tag
git add -A && git commit -m "chore(release): vX.Y.Z (kind)"
git tag -a vX.Y.Z -m "Release vX.Y.Z"
git push origin main vX.Y.Z
```

`scripts/bump_version.py --dry-run` reports what it *would* do without
touching any files.
