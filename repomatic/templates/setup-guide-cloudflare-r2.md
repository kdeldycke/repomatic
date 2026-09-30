---
args: [bucket, repo_slug]
footer: 'false'
---

`[tool.repomatic] site.cloudflare-r2-bucket` declares `$bucket`, so the deploy moves every file over 25 MiB to that bucket. Without the two keys below, it drops those files instead, and their links break.

1. Create the bucket and attach its domain, from a machine logged in with `wrangler login`. A re-run is safe:

   ```shell
   repomatic cloudflare-r2 --create
   ```

2. Create an account API token with the **Object Read & Write** permission, limited to the `$bucket` bucket. The account token form lists that permission as **Workers R2 Storage Bucket Item Read** and **Write**.

3. Store the token's two S3 values, the Access Key ID and the Secret Access Key, as repository secrets. When the form shows only a token value, the Access Key ID is the token's ID and the Secret Access Key is the SHA-256 of the value:

   ```shell
   gh secret set CLOUDFLARE_R2_ACCESS_KEY_ID --repo $repo_slug
   gh secret set CLOUDFLARE_R2_SECRET_ACCESS_KEY --repo $repo_slug
   ```

> [!NOTE]
> The keys reach the `$bucket` bucket only. Cloudflare limits a credential to one bucket on its S3 API alone, which is why uploads do not use `CLOUDFLARE_API_TOKEN`. See [files over 25 MiB](https://repomatic.net/cloudflare#files-over-25-mib).
