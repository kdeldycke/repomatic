---
args: [repo_url, repo_slug]
footer: 'false'
---

Enable vulnerability alerts so `fix-vulnerable-deps` can read them, and disable automated security fixes so Dependabot stops opening duplicate PRs:

1. Run both calls:

   ```shell
   gh api repos/$repo_slug/vulnerability-alerts --method PUT
   gh api repos/$repo_slug/automated-security-fixes --method DELETE
   ```

2. Turn off **Dependabot version updates** and **Grouped security updates** at **[Settings → Advanced Security → Dependabot]($repo_url/settings/security_analysis)**. No API reaches either setting.

3. Delete `.github/dependabot.yml` if the repository has one: `sync-uv-lock`, `sync-tool-versions` and `sync-action-pins` cover dependency updates.
