---
args: [repo_url, repo_name, repo_owner, repo_slug]
footer: 'false'
---

Some jobs create PRs touching `.github/workflows/`, which needs a **fine-grained personal access token**. Without it they fail silently.

1. Open the [**pre-filled token form**](https://github.com/settings/personal-access-tokens/new?name=$repo_name-repomatic&description=REPOMATIC_PAT+for+$repo_owner/$repo_name&target_name=$repo_owner&administration=read&contents=write&issues=write&metadata=read&pull_requests=write&vulnerability_alerts=read&workflows=write), which arrives named `$repo_name-repomatic` with every permission below already selected.

2. Under **Repository access**, choose **Only select repositories** and pick **\$repo_name**, and nothing else.

3. Check the permissions match:

   | Permission            | Access                  |
   | :-------------------- | :---------------------- |
   | **Administration**    | Read-only               |
   | **Contents**          | Read and Write          |
   | **Dependabot alerts** | Read-only               |
   | **Issues**            | Read and Write          |
   | **Metadata**          | Read-only *(mandatory)* |
   | **Pull requests**     | Read and Write          |
   | **Workflows**         | Read and Write          |

4. Set an expiration, and a reminder that outlives it: an expired token fails jobs silently.

5. Click **Generate token**, then store it:

   ```shell
   gh secret set REPOMATIC_PAT --repo $repo_slug
   ```

   Or by hand: **[Settings → Secrets → Actions]($repo_url/settings/secrets/actions)** → **New repository secret** → `REPOMATIC_PAT`.
