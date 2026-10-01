---
args: [repo_url, repo_slug]
footer: 'false'
---

Set the GitHub Pages deployment source to **GitHub Actions**, which is how `docs.yaml` deploys:

1. Run the call that fits: `POST` enables Pages for the first time, `PUT` changes an existing setup.

   ```shell
   gh api --method POST repos/$repo_slug/pages -f build_type=workflow
   gh api --method PUT repos/$repo_slug/pages -f build_type=workflow
   ```

   Or by hand: **[Settings → Pages]($repo_url/settings/pages)** → **Build and deployment** → **Source** → **GitHub Actions**.

2. Delete a `gh-pages` branch left over from an older deployment method:

   ```shell
   gh api --method DELETE repos/$repo_slug/git/refs/heads/gh-pages
   ```
