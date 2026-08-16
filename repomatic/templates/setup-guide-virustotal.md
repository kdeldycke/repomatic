---
args: [repo_url, repo_slug]
footer: 'false'
---

Optional. Submitting release binaries to VirusTotal seeds AV vendor databases and reduces false positives for downstream distributors. Without the key, releases skip the scan.

1. Sign in to [**VirusTotal**](https://www.virustotal.com/gui/my-apikey), where a free account is enough.

2. Copy the **API key** from the account page.

3. Add it as a repository secret:

   ```shell
   gh secret set VIRUSTOTAL_API_KEY --repo $repo_slug
   ```

   Or by hand: **[Settings → Secrets → Actions]($repo_url/settings/secrets/actions)** → **New repository secret** → `VIRUSTOTAL_API_KEY`.

> [!IMPORTANT]
> With the key set, each release also commits its scan results and the refreshed `docs/binaries.md` straight to the default branch, no pull request: see the [rationale](https://kdeldycke.github.io/repomatic/operation-contracts.html#release-lane-direct-commits). Keep the scan without the commit by setting `binaries.sync = false` in `[tool.repomatic]`.
