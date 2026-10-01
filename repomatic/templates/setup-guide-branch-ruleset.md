---
args: [repo_url]
footer: 'false'
---

Create a [**branch ruleset**]($repo_url/settings/rules/new?target=branch&enforcement=active) so the default branch cannot be force-pushed or deleted:

1. **Ruleset name**: `main`
2. **Enforcement status**: Active
3. Under **Target branches**: **Add target** → **Include default branch**
4. Keep **Restrict deletions** and **Block force pushes** checked
5. Click **Create**
