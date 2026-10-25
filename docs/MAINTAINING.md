# Maintaining this hobby project

## Normal work

1. Work on `develop`, or merge a small feature branch into it.
2. Push and read the checks under **Actions**. Green means the configured
   checks passed; it does not prove every provider still works live.
3. Open a `develop` → `main` PR when ready to release. Wait for the checks,
   inspect the changes, and merge it. You can merge your own PR.
4. Wait for **Release** to finish. It chooses the next patch version and
   publishes the downloads. You do not need to edit a version or create a tag.
5. Bring `main` back into `develop` after a release or a bot fix. If `develop`
   has no extra commits, `git switch develop` followed by
   `git merge --ff-only origin/main` and `git push origin develop` is enough.

Contributor and Dependabot PRs may also target `main`. Check them before
merging and bring the resulting commits back into `develop` afterward.
Every merge to `main`, including a documentation change, currently triggers
a release. A manual release button is an optional future preference.

## Branch rules

In the existing `main` ruleset, require **Quality and Python package** and
**Device integration (Linux)**. Keep the required approving-review count at
zero. Keep force pushes and branch deletion blocked. Desktop checks only run
for packaging changes, so requiring them for every PR would block other PRs.

Administrator bypass is an emergency escape hatch. The recommended compromise
is to change the administrator entry from **Always allow** to **For pull
requests only**. This preserves intentional emergency merges while requiring
a PR record and preventing direct pushes through that bypass. Normally wait
for checks even when bypass is available.

An administrator-role bypass also applies to future administrators. Give
contributors write access unless they actually need to manage settings. In
an organization, the maintain role is another option. If more administrators
join, reconsider the broad administrator bypass before granting that access.
Only trusted people should be administrators; they can change the rules.

## Free automation and reports

The workflows use standard GitHub-hosted runners, which are free for public
repositories. Temporary release artifacts expire after three days. Packaging
PR checks keep no uploaded binaries. Actual release downloads remain available.

Review **Billing → Budgets and alerts** in your own account. For no paid
Actions usage, use a $0 Actions budget with **Stop usage when budget limit is
reached** enabled. An alert-only budget does not stop spending. Optional AI
features have their own usage rules; these workflows require no AI subscription.

Dependabot checks Python and Actions dependencies monthly and groups updates.
Security updates are separate and may arrive earlier. CodeQL scans Python and
workflow code. Read alerts in **Security and quality**; a completed scan does
not mean there are no findings. Review bot PRs before merging them.

Bug and feature forms live under **Issues → New issue**. Use **Discussions**
for questions. Ask for a small reproducible example and redacted logs; never
request real conversation archives or browser profiles. Private vulnerability
reporting and the [security policy](../SECURITY.md) cover security reports.

There is no guaranteed response time or obligation to implement every request.
You do not need an automatic issue-closing or AI triage bot for a small project.
