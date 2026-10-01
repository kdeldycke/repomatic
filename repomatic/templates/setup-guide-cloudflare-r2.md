---
args: [bucket, domain, repo_slug, token_name]
footer: 'false'
---

`[tool.repomatic] site.cloudflare-r2-bucket` is `$bucket`, so the deploy moves [files over 25 MiB](https://repomatic.net/cloudflare#files-over-25-mib) to that bucket. The bucket and the keys below are both required: without either, the deploy drops those files and fails.

1. Create the R2 bucket, named `$bucket`, served at `$domain`. Nothing else creates it:

   ```shell
   repomatic cloudflare-r2 --create
   ```

   Or create it another way:

   - `wrangler r2 bucket create $bucket`, then `wrangler r2 bucket domain add $bucket --domain $domain --zone-id {zone-id} --min-tls 1.2`.
   - The dashboard: **[R2 object storage](https://dash.cloudflare.com/?to=/:account/r2/overview)** to create the bucket, then its **Settings** → **Custom Domains** → **Add**.

   > ⚠️ **Warning**: The dashboard cannot set the TLS floor. Run `repomatic cloudflare-r2 --create` once afterwards to raise it to 1.2.

2. Create the API token in the **[R2 account token form](https://dash.cloudflare.com/?to=/:account/r2/api-tokens/create&type=account)**. A link cannot pre-fill this form, so change each field from its default:

   | Field                 | Value                                          |
   | :-------------------- | :--------------------------------------------- |
   | **Token name**        | `$token_name`                                  |
   | **Permissions**       | Object Read & Write                            |
   | **Specify bucket(s)** | Apply to specific buckets only, then `$bucket` |
   | **TTL**               | 1 year                                         |

   The generic **[account token form](https://dash.cloudflare.com/?to=/:account/api-tokens/create)** works too: grant **Workers R2 Storage Bucket Item Write**, scoped to `$bucket` under **R2 Buckets**.

3. Store the token's Access Key ID and Secret Access Key as repository secrets:

   ```shell
   gh secret set CLOUDFLARE_R2_ACCESS_KEY_ID --repo $repo_slug
   gh secret set CLOUDFLARE_R2_SECRET_ACCESS_KEY --repo $repo_slug
   ```

> ℹ️ **Note**: To go back, remove `site.cloudflare-r2-bucket` and `site.cloudflare-r2-domain`. The deploy then fails on any file over 25 MiB.
