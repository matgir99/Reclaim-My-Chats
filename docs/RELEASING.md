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
version override before creating the tag. It checks their metadata and the
Linux, Windows, and macOS desktop archives. Only then does it create or reuse
the tag, create a draft release, upload and verify assets, and publish it.
Rerunning a failed workflow at the same `main` commit reuses its tag and
release. If `main` has advanced, use the Release workflow's manual trigger on
the current `main` commit; it will not tag an older commit. No dummy commit is
needed.

`main` remains the public default branch and should require a pull request,
the CI checks, and the Flet device test. Direct and force pushes and branch
deletion should remain disabled. The `develop` branch is for ordinary work.
