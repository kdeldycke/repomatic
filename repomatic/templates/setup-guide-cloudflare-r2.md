---
args: [bucket, domain, repo_slug, token_name]
footer: 'false'
---

`[tool.repomatic] site.cloudflare-r2-bucket` is `$bucket`, so the deploy moves [files over 25 MiB](https://repomatic.net/cloudflare#files-over-25-mib) there. The bucket and the keys are both required: without either, the deploy drops those files and fails.

1. Create the R2 bucket, named `$bucket`, served at `$domain`. Nothing else creates it:

   ```shell
   repomatic cloudflare-r2 --create
   ```

   Or by hand: `wrangler r2 bucket create $bucket`, then `wrangler r2 bucket domain add $bucket --domain $domain --zone-id {zone-id} --min-tls 1.2`.

2. Create the API token in the **[R2 account token form](https://dash.cloudflare.com/?to=/:account/r2/api-tokens/create&type=account)**. No link pre-fills this form, so set each field:

   | Field                 | Value                                          |
   | :-------------------- | :--------------------------------------------- |
   | **Token name**        | `$token_name`                                  |
   | **Permissions**       | Object Read & Write                            |
   | **Specify bucket(s)** | Apply to specific buckets only, then `$bucket` |
   | **TTL**               | 1 year                                         |

3. Store the token's Access Key ID and Secret Access Key as repository secrets:

   ```shell
   gh secret set CLOUDFLARE_R2_ACCESS_KEY_ID --repo $repo_slug
   gh secret set CLOUDFLARE_R2_SECRET_ACCESS_KEY --repo $repo_slug
   ```
