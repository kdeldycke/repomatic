---
args: [repo_name, repo_owner, repo_slug, repo_url, token_name]
footer: 'false'
---

`[tool.repomatic] site.deploy` is set to `cloudflare-pages`, so the built site ships with `wrangler pages deploy` instead of `actions/deploy-pages`. That path authenticates with a stored token, and fails the deploy outright when either value below is missing.

1. Create the Pages project, named `$repo_name` unless `site.cloudflare-project` says otherwise. The deploy job never creates it: `wrangler pages deploy` only uploads into a project that already exists, so this step comes first. From a terminal, with either a `wrangler login` session or a token already in hand:

   ```shell
   repomatic cloudflare-pages --create
   ```

   That creates it as Direct Upload and applies whatever `site.cloudflare-*` declares in the same pass. To create it and nothing else, `wrangler pages project create $repo_name --production-branch=main` does that much. Or in the dashboard: **[Workers & Pages](https://dash.cloudflare.com/?to=/:account/workers-and-pages/create/pages)** → **Direct Upload** → **Create project**, skipping the upload it then offers, since the workflow supplies the files.

2. Create the API token with the **[pre-filled token form](https://dash.cloudflare.com/?to=/:account/api-tokens&permissionGroupKeys=%5B%7B%22key%22%3A%22page%22%2C%22type%22%3A%22edit%22%7D%5D&name=$token_name)**, which arrives account-owned, named `$token_name`, with **Account → Cloudflare Pages → Edit** already selected. The name carries the month because Cloudflare's token list shows what a token can do and never how old it is, which is what a one-year expiry and a rotation both turn on. Account-owned rather than the tokens under *My Profile*: those are user-owned, die with the user, and their creation form defaults its permission dropdown to *User*, where Cloudflare Pages does not even appear. The one thing the URL cannot carry: give it a **one-year TTL** by hand.

   Do not expect to narrow it further. `Cloudflare Pages` is an account permission, scoped `com.cloudflare.api.account`, so a token carrying it can edit **every** Pages project on the account, and neither a single project nor a single domain can be selected as its resource. The account is the only boundary Cloudflare offers here; see the [token model](https://kdeldycke.github.io/repomatic/cloudflare.html#the-token) for what follows from that.

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
> If this project ever published to `$repo_owner.github.io/$repo_name`, leave GitHub Pages **enabled** and set its custom domain to the new one: GitHub then redirects every old URL to it with a path-preserving `301`, at no cost and with no files to maintain. Disabling Pages instead deletes that redirect and every historical link with it. `lint-repo` checks this, and the [migration guide](https://kdeldycke.github.io/repomatic/cloudflare.html#leaving-github-pages-behind-urls-intact) explains it.

> [!NOTE]
> To go back to GitHub Pages, drop `site.deploy` from `[tool.repomatic]` or set it to `github-pages`. Both secrets then go unread, and this step disappears.
