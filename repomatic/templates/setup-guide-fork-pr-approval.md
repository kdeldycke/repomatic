---
args: [repo_url, repo_slug]
footer: 'false'
---

Require approval before a fork pull request runs workflows. GitHub's default catches only a brand-new account.

```shell
gh api --method PUT repos/$repo_slug/actions/permissions/fork-pr-contributor-approval -f approval_policy=first_time_contributors
```

Or by hand: **[Settings → Actions → General]($repo_url/settings/actions)** → **Fork pull request workflows from outside collaborators** → **Require approval for first-time contributors**.
