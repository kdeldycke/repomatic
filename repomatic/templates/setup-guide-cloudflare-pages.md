---
args: [repo_name, repo_owner, repo_slug, repo_url, token_name]
footer: 'false'
---

`[tool.repomatic] site.deploy` is `cloudflare-pages`, so the site ships with `wrangler pages deploy`. Both values below are required: without either, the deploy fails rather than skips.

1. Create the Pages project, named `$repo_name`. Nothing else creates it, since `wrangler pages deploy` only uploads into a project that exists:

   ```shell
   repomatic cloudflare-pages --create
   ```

   Or `wrangler pages project create $repo_name --production-branch=main`, or the dashboard: **[Workers & Pages](https://dash.cloudflare.com/?to=/:account/workers-and-pages/create/pages)** → **Direct Upload**.

2. Create the API token from the **[pre-filled form](https://dash.cloudflare.com/?to=/:account/api-tokens&permissionGroupKeys=%5B%7B%22key%22%3A%22page%22%2C%22type%22%3A%22edit%22%7D%5D&name=$token_name)**: account-owned, named `$token_name`, carrying **Account → Cloudflare Pages → Edit**. Add a **one-year TTL** by hand, the one thing the URL cannot carry.

3. Store both as repository secrets. The account ID needs no copying:

   ```shell
   gh secret set CLOUDFLARE_API_TOKEN --repo $repo_slug
   repomatic cloudflare-pages --account-id | gh secret set CLOUDFLARE_ACCOUNT_ID --repo $repo_slug
   ```

   Paste each value into the secret that names it. `gh secret set` prompts blind, so a token stored as the account ID is an easy slip, and it fails as a `404` naming neither.

> [!NOTE]
> Two things worth knowing, both covered in the [Cloudflare Pages guide](https://kdeldycke.github.io/repomatic/cloudflare.html): the token reaches **every** Pages project on the account, since `Cloudflare Pages` is an account permission that cannot be narrowed to one project or domain; and if this project ever published to `$repo_owner.github.io/$repo_name`, leaving GitHub Pages enabled with its custom domain set to the new one is what keeps those URLs redirecting.

> [!NOTE]
> To go back, set `site.deploy = "github-pages"`. Both secrets go unread and this step disappears.
