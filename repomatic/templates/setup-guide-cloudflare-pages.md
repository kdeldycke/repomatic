---
args: [repo_name, repo_slug, repo_url]
footer: 'false'
---

`[tool.repomatic] sphinx.deploy` is set to `cloudflare-pages`, so the Docs workflow uploads the built site with `wrangler pages deploy` instead of `actions/deploy-pages`. That path authenticates with a stored token, and fails the deploy job outright when either value below is missing.

1. Create the Pages project first, named exactly after this repository: **[Cloudflare dashboard → Workers & Pages](https://dash.cloudflare.com/?to=/:account/workers-and-pages/create/pages)** → **Direct Upload** → name it `$repo_name` → **Create project**. Skip the upload it then offers, since the workflow supplies the files. `wrangler` deploys into a project that already exists and will not create one non-interactively.

2. Create an API token at **[My Profile → API Tokens](https://dash.cloudflare.com/profile/api-tokens)** → **Create Token** → **Create Custom Token**. Grant it **Account → Cloudflare Pages → Edit** and nothing else, restricted to the account owning the project.

3. Copy the **Account ID** from the right-hand sidebar of any dashboard page, or from the URL: `dash.cloudflare.com/{account_id}/...`.

4. Add both as repository secrets:

```shell
gh secret set CLOUDFLARE_API_TOKEN --repo $repo_slug
gh secret set CLOUDFLARE_ACCOUNT_ID --repo $repo_slug
```

Or add them manually: **this repo → [Settings → Secrets → Actions]($repo_url/settings/secrets/actions)** → **New repository secret**.

> [!IMPORTANT]
> Give the token an expiry, and put a reminder somewhere that outlives this issue. Cloudflare warns about neither an approaching expiry nor a lapsed one, so the first symptom is a red Docs run on a day nobody is looking.

> [!NOTE]
> To go back to GitHub Pages, drop `sphinx.deploy` from `[tool.repomatic]` or set it to `github-pages`. Both secrets then go unread, and this step disappears.
