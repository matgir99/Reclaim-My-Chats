# Releasing

1. Check that `develop` passes CI and the Flet GUI tests.
2. Open a pull request from `develop` to `main`.
3. Merge it after the required checks pass.
4. The Release workflow builds and checks all packages, tags the commit, and
   publishes a GitHub Release with those packages and generated notes.

The first GUI release is `v3.3.0`. Later releases increment the patch
component of the highest reachable release tag. There is no version edit or
commit-message convention. `setuptools-scm` gives development commits a
development version and release artifacts the exact tagged version.

The workflow builds the Python wheel and source distribution with an explicit
version override before creating the tag. It checks their metadata, installs
the wheel in a fresh environment, and launches each extracted Linux, Windows,
and macOS application with isolated data. This also starts the bundled
Playwright driver without opening a browser or contacting a provider.
Only then does it create or reuse
the tag, create a draft release, upload and verify assets, and publish it.
Rerunning a failed workflow at the same `main` commit reuses its tag and
resumes an incomplete draft. Published release downloads are never replaced.
Draft lookup uses the authenticated release list when GitHub's tag endpoint
does not expose the draft. Ambiguous drafts and API errors stop publication.
Uploads are checked against GitHub's SHA-256 digests before publication.
Temporary workflow packages expire after three days; the downloads on the
Release page remain available. After temporary artifacts expire, rerun all
jobs to rebuild them. If `main` has advanced, use the Release workflow's manual trigger on
the current `main` commit; it will not tag an older commit. No dummy commit is
needed.

`main` remains the public default branch. Require **Quality and Python
package** and **Device integration (Linux)** before merging; zero approving
reviews is appropriate for a solo maintainer. Desktop packaging checks run
on PRs that change packaging and on manual requests, and all three platforms
are checked for every release. Do not require a check that only runs for
selected paths. See [maintainer settings](MAINTAINING.md) for administrator bypass.

All contributor and bot changes must merge into `develop` first; only this repository's `develop` PR may merge into `main`. The required **Quality and Python package** check enforces that PR source. Dependabot version updates target `develop`; retarget any default-branch security update PRs to `develop` before merging. Synchronize `main` back into `develop` after a release. Keep release merges sequential:
wait for one release to finish before merging another release PR.
