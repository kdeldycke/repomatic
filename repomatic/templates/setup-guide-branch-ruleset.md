---
args: [repo_url]
footer: 'false'
---

Create a [**branch ruleset**]($repo_url/settings/rules/new?target=branch&enforcement=active) so the default branch cannot be force-pushed or deleted:

1. **Ruleset name**: `main`
2. **Enforcement status**: Active
3. Under **Target branches**: **Add target** → **Include default branch**
4. Check **Restrict deletions** and **Block force pushes**, both on by default
5. Click **Create**

No status checks needed.
