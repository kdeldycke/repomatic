---
args: [repo_name, repo_slug, repo_url]
footer: 'false'
---

`[tool.repomatic] site.deploy` is set to `cloudflare-pages`, so the built site ships with `wrangler pages deploy` instead of `actions/deploy-pages`. That path authenticates with a stored token, and fails the deploy outright when either value below is missing.

1. Create the Pages project first, named `$repo_name` unless `site.cloudflare-project` says otherwise: **[Cloudflare dashboard → Workers & Pages](https://dash.cloudflare.com/?to=/:account/workers-and-pages/create/pages)** → **Direct Upload** → **Create project**. Skip the upload it then offers, since the workflow supplies the files. `wrangler` deploys into a project that already exists and will not create one non-interactively; with a token already in hand, `repomatic cloudflare-pages --create` does this step and the next configuration pass from a terminal.

2. Create the API token with the **[pre-filled token form](https://dash.cloudflare.com/?to=/:account/api-tokens&permissionGroupKeys=%5B%7B%22key%22%3A%22page%22%2C%22type%22%3A%22edit%22%7D%5D&name=$repo_name-deploy)**, which arrives account-owned with **Account → Cloudflare Pages → Edit** and the name already set. Account-owned rather than the tokens under *My Profile*: those are user-owned, die with the user, and their creation form defaults its permission dropdown to *User*, where Cloudflare Pages does not even appear. Two things the URL cannot carry, so set them by hand: append `-{YYYY-MM}` to the name so the token says when it was made, and give it a **one-year TTL**.

3. Copy the **Account ID** from the right-hand sidebar of any dashboard page, or from the URL: `dash.cloudflare.com/{account_id}/...`.

4. Add both as repository secrets:

```shell
gh secret set CLOUDFLARE_API_TOKEN --repo $repo_slug
gh secret set CLOUDFLARE_ACCOUNT_ID --repo $repo_slug
```

Or add them manually: **this repo → [Settings → Secrets → Actions]($repo_url/settings/secrets/actions)** → **New repository secret**.

> [!NOTE]
> The TTL bounds the damage of a leak, and nothing more is needed to survive it: Cloudflare warns about neither an approaching expiry nor a lapsed one, but the Docs workflow's monthly run turns a dead token into a red run and an email, and its drift check starts warning a month ahead. When the warning fires, follow the [rotation runbook](https://kdeldycke.github.io/repomatic/cloudflare.html#rotating-the-token): create the replacement first, verify it with a real deploy, only then revoke the incumbent.

> [!NOTE]
> To go back to GitHub Pages, drop `site.deploy` from `[tool.repomatic]` or set it to `github-pages`. Both secrets then go unread, and this step disappears.
