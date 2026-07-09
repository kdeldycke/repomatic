---
args: [repo_url, repo_slug]
footer: 'false'
---

Release binaries compiled with [Nuitka](https://nuitka.net/) often trigger false positives on VirusTotal. Submitting binaries proactively on each release seeds AV vendor databases and reduces detection counts for downstream distributors (Chocolatey, Scoop, etc.).

1. Go to [**VirusTotal**](https://www.virustotal.com/gui/my-apikey) and sign in (a free account is sufficient).

2. Copy your **API key** from the account page.

3. Add it as a repository secret:

```shell
gh secret set VIRUSTOTAL_API_KEY --repo $repo_slug
```

Or add it manually: **this repo → [Settings → Secrets → Actions]($repo_url/settings/secrets/actions)** → **New repository secret** → name it `VIRUSTOTAL_API_KEY` → paste the key.

> [!NOTE]
> This step is optional. Without the key, release workflows skip the VirusTotal scan. The free-tier API allows 4 requests per minute, which is sufficient for typical release binaries.

> [!IMPORTANT]
> With the key configured, each release also records its scan results and the refreshed binaries catalog (`docs/binaries.md`) with a commit pushed directly to the default branch (no pull request): see the [rationale](https://kdeldycke.github.io/repomatic/operation-contracts.html#release-lane-direct-commits). To keep the scan but skip that commit, set `binaries.sync = false` in `[tool.repomatic]`.
