---
args: [repo_url, repo_slug]
footer: 'false'
---

Have GitHub refuse to run any workflow referencing an action by tag or branch instead of a full commit SHA. `enabled` is required on this endpoint, so read the settings and write them back with only the one field flipped:

```shell
gh api repos/$repo_slug/actions/permissions --jq '. + {sha_pinning_required: true}' \
  | gh api --method PUT repos/$repo_slug/actions/permissions --input -
```

Or by hand: **[Settings → Actions → General]($repo_url/settings/actions)** → **Actions permissions** → **Require actions to be pinned to a full-length commit SHA**.
