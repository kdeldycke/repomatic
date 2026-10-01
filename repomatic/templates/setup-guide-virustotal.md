---
args: [repo_url, repo_slug]
footer: 'false'
---

Optional. Scanning release binaries on VirusTotal seeds AV vendor databases and cuts false positives. Without the key, releases skip the scan.

1. Sign in to [**VirusTotal**](https://www.virustotal.com/gui/my-apikey): a free account is enough.

2. Copy the **API key** from the account page.

3. Store it as a repository secret:

   ```shell
   gh secret set VIRUSTOTAL_API_KEY --repo $repo_slug
   ```

   Or by hand: **[Settings → Secrets → Actions]($repo_url/settings/secrets/actions)** → **New repository secret** → `VIRUSTOTAL_API_KEY`.

> ℹ️ **Note**: With the key set, each release appends its scan results to one long-lived pull request, which you merge when it suits you. Set `[tool.repomatic] binaries.sync = false` to keep the scan without that pull request.
