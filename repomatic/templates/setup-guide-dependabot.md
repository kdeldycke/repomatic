---
args: [repo_url, repo_slug]
footer: 'false'
---

Enable [vulnerability alerts](https://docs.github.com/en/code-security/dependabot/dependabot-alerts/configuring-dependabot-alerts) so the `fix-vulnerable-deps` job can read them and open security PRs, and [disable automated security fixes](https://docs.github.com/en/code-security/dependabot/dependabot-security-updates/configuring-dependabot-security-updates) so Dependabot does not open duplicate PRs for the same advisories:

```shell
gh api repos/$repo_slug/vulnerability-alerts --method PUT
gh api repos/$repo_slug/automated-security-fixes --method DELETE
```

Disabling security updates also disables grouped security updates. Dependabot version updates and grouped security updates have no API — if either was manually enabled, disable them at **this repo → [Settings → Advanced Security → Dependabot]($repo_url/settings/security_analysis)**. Remove `.github/dependabot.yml` if present: repomatic's `sync-uv-lock`, `sync-tool-versions`, and `sync-action-pins` jobs handle dependency updates.
