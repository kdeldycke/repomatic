---
args: [repo_name, repo_owner, repo_slug, repo_url, token_name]
footer: 'false'
---

`[tool.repomatic] site.deploy` is `cloudflare-pages`, so the site ships with `wrangler pages deploy`. The project and the token below are both required: without either, the deploy fails rather than skips.

1. Create the Pages project, named `$repo_name`. Nothing else creates it:

   ```shell
   repomatic cloudflare-pages --create
   ```

   Or `wrangler pages project create $repo_name --production-branch=main`, or the dashboard: **[Workers & Pages](https://dash.cloudflare.com/?to=/:account/workers-and-pages/create/pages)** → **Direct Upload**.

2. Create the account-owned API token from the **[pre-filled form](https://dash.cloudflare.com/?to=/:account/api-tokens&permissionGroupKeys=%5B%7B%22key%22%3A%22page%22%2C%22type%22%3A%22edit%22%7D%5D&name=$token_name)**, then check the values:

   | Field                | Value                             |
   | :------------------- | :-------------------------------- |
   | **Token name**       | `$token_name`                     |
   | **Permissions**      | Account → Cloudflare Pages → Edit |
   | **Token expiration** | 1 year *(set by hand)*            |

3. Store the token as a repository secret:

   ```shell
   gh secret set CLOUDFLARE_API_TOKEN --repo $repo_slug
   ```

> ℹ️ **Note**: The token reaches **every** Pages project on the account: `Cloudflare Pages` is an account permission that cannot be narrowed to one project. If this project ever published to `$repo_owner.github.io/$repo_name`, leave GitHub Pages enabled there with its custom domain set to the new site: the old URLs then keep redirecting. The [Cloudflare Pages guide](https://repomatic.net/cloudflare) covers both.

> ℹ️ **Note**: To go back, set `site.deploy = "github-pages"`.
