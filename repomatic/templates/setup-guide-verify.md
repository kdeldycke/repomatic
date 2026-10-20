---
args: [repo_url, repo_slug]
footer: false
---

Trigger a workflow re-run:

```shell
gh workflow run autofix.yaml --repo $repo_slug
```

Or re-run from the [**Actions tab**]($repo_url/actions/workflows/autofix.yaml). Jobs should now update `.github/workflows/` files without errors.
