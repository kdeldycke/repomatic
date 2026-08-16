---
args: [repo_url, repo_slug]
footer: 'false'
---

Enable [vulnerability alerts](https://docs.github.com/en/code-security/dependabot/dependabot-alerts/configuring-dependabot-alerts) so `fix-vulnerable-deps` can read them, and disable [automated security fixes](https://docs.github.com/en/code-security/dependabot/dependabot-security-updates/configuring-dependabot-security-updates) so Dependabot stops opening duplicate PRs for the same advisories:

```shell
gh api repos/$repo_slug/vulnerability-alerts --method PUT
gh api repos/$repo_slug/automated-security-fixes --method DELETE
```

Then two things the API cannot reach. Dependabot version updates and grouped security updates have no endpoint: if either was enabled by hand, turn it off at **[Settings → Advanced Security → Dependabot]($repo_url/settings/security_analysis)**. And delete `.github/dependabot.yml` if present, since `sync-uv-lock`, `sync-tool-versions` and `sync-action-pins` cover dependency updates.
