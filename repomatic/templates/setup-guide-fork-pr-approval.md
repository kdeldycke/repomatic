---
args: [repo_url, repo_slug]
footer: 'false'
---

Require approval before a fork pull request runs workflows, for any first-time contributor to this repository. GitHub's default only catches brand-new accounts.

```shell
gh api --method PUT repos/$repo_slug/actions/permissions/fork-pr-contributor-approval -f approval_policy=first_time_contributors
```

Or by hand: **[Settings → Actions → General]($repo_url/settings/actions)** → **Fork pull request workflows from outside collaborators** → **Require approval for first-time contributors**. Pick `all_external_contributors` instead to require it for every outside PR.
