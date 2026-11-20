# Releasing

1. Check that `develop` passes CI and the Flet GUI tests.
2. Run the live check from a `develop` checkout with logged-in providers:
   `reclaim doctor --live`, or **Diagnostics → Run live check** in the
   window. Every enabled provider must log in and list its chats. CI cannot
   do this, because it has no provider accounts; a provider that fails here
   has changed its service, so fix it before releasing.
3. Open a pull request from `develop` to `main` and tick the live check in
   its checklist.
4. Merge it after the required checks pass.
5. The Release workflow builds and checks all packages, tags the commit, and
   publishes a GitHub Release with those packages and generated notes.

The first GUI release is `v3.3.0`. Later releases increment the patch
component of the highest reachable release tag. There is no version edit or
commit-message convention. `setuptools-scm` gives development commits a
development version and release artifacts the exact tagged version.

The workflow builds the Python wheel and source distribution with an explicit
version override before creating the tag. It checks their metadata, installs
the wheel in a fresh environment and uses the command line from it in an empty
home (`scripts/smoke_wheel.py`: version, `init`, `status`; CI runs the same
script on every pull request), and builds desktop downloads using only the
`gui-pack` dependencies: Linux `.tar.gz`, `.AppImage`, and `.deb` on Ubuntu
22.04 (`glibc 2.35`), Windows portable `.zip` and per-user `-setup.exe`
installer, and macOS Apple Silicon (`arm64`) and Intel (`x86_64`) `.dmg` and
`.zip` bundles. Each packaged application is launched in isolation with
`scripts/smoke_desktop.py` (which also starts the bundled Playwright driver
without opening a browser or contacting a provider).
Only then does it generate `SHA256SUMS` and GitHub build-provenance
attestations, create or reuse the tag, create a draft release, upload and
verify assets, and publish the GitHub Release, which carries the Python wheel
and source distribution. Uploading them to PyPI is switched off (decided on 20
November 2026): that step, `pypa/gh-action-pypi-publish` with Trusted
Publishing, runs only when the repository variable `PYPI_PUBLISH` is `true`,
so a release shows it as skipped. To turn it on, register a Trusted Publisher
on `pypi.org` for the project `reclaimmychats` (workflow `release.yml`,
environment left blank), then set the variable under **Settings → Secrets and
variables → Actions → Variables**. The step keeps `continue-on-error: true`,
so a PyPI problem never blocks the GitHub Release.
Ubuntu packages for the Linux desktop build (and the Flet test job) are
installed with `scripts/apt_install.sh`: fetching them gets a time limit and up
to three attempts, switching from the runner's Azure mirror to
`archive.ubuntu.com` after the first. That mirror stalled several times in
November 2026 and used up a whole 25-minute job.

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
