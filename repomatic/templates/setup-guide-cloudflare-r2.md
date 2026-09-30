---
args: [bucket, domain, repo_slug, token_name]
footer: 'false'
---

`[tool.repomatic] site.cloudflare-r2-bucket` is `$bucket`, so the deploy moves [files over 25 MiB](https://repomatic.net/cloudflare#files-over-25-mib) to that bucket. The bucket and the keys below are both required: without either, the deploy drops those files and fails.

1. Create the R2 bucket, named `$bucket`, served at `$domain`. Nothing else creates it:

   ```shell
   repomatic cloudflare-r2 --create
   ```

   Or `wrangler r2 bucket create $bucket` then `wrangler r2 bucket domain add $bucket --domain $domain --zone-id {zone-id} --min-tls 1.2`, or the dashboard: **[R2 object storage](https://dash.cloudflare.com/?to=/:account/r2/overview)** to create the bucket, then its **Settings** → **Custom Domains** → **Add**.

   > [!WARNING]
   > The dashboard cannot set the TLS floor. Run `repomatic cloudflare-r2 --create` once afterwards to raise it to 1.2.

2. Create the API token from **[R2 object storage](https://dash.cloudflare.com/?to=/:account/r2/overview)** → **Account Details** → **Manage** next to **API Tokens** → **Create Account API token**: named `$token_name`, carrying **Object Read & Write**, applied to the `$bucket` bucket only. Give it a **one-year** TTL. The generic **[account API tokens](https://dash.cloudflare.com/?to=/:account/api-tokens)** form works too, where that permission reads **Workers R2 Storage Bucket Item Read** and **Write**.

3. Store the token's Access Key ID and Secret Access Key as repository secrets:

   ```shell
   gh secret set CLOUDFLARE_R2_ACCESS_KEY_ID --repo $repo_slug
   gh secret set CLOUDFLARE_R2_SECRET_ACCESS_KEY --repo $repo_slug
   ```

   When the form shows only a token value, the Access Key ID is the token's ID and the Secret Access Key is the SHA-256 of the value. No other secret is needed: the account is derived from `CLOUDFLARE_API_TOKEN`.

> [!NOTE]
> To go back, remove `site.cloudflare-r2-bucket` and `site.cloudflare-r2-domain`. The R2 secrets go unread and this step disappears, but the deploy then fails on any file over 25 MiB.
