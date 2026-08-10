---
args: [repo_url, repo_name, repo_owner, repo_slug]
footer: 'false'
---

Some workflows need a **fine-grained personal access token** to create PRs that update files in `.github/workflows/`. Without it, those jobs will silently fail.

1. Open the [**pre-filled token form**](https://github.com/settings/personal-access-tokens/new?name=$repo_name-repomatic&description=REPOMATIC_PAT+for+$repo_owner/$repo_name&target_name=$repo_owner&administration=read&contents=write&issues=write&metadata=read&pull_requests=write&vulnerability_alerts=read&workflows=write) (or go to **GitHub → Settings → Developer Settings → [Fine-grained tokens](https://github.com/settings/personal-access-tokens)** and click **Generate new token**).

2. Review the pre-filled **Token name** (`$repo_name-repomatic`).

3. Under **Repository access**, select **Only select repositories** and pick **\$repo_name**. Do not grant access to other repositories.

4. Verify these permissions (pre-filled by the link above):

   | Permission            | Access                  | Reason                                                                                    |
   | :-------------------- | :---------------------- | :---------------------------------------------------------------------------------------- |
   | **Administration**    | Read-only               | Read the Actions settings this guide verifies: SHA pinning, fork PR approval policy       |
   | **Contents**          | Read and Write          | Tag pushes, release publishing, PR branch creation                                        |
   | **Dependabot alerts** | Read-only               | fix-vulnerable-deps reads vulnerability alerts to create security PRs                     |
   | **Issues**            | Read and Write          | Setup guide issue management                                                              |
   | **Metadata**          | Read-only *(mandatory)* | Required for all fine-grained token API operations                                        |
   | **Pull requests**     | Read and Write          | All PR-creating jobs (sync-repomatic, fix-typos, sync-uv-lock, sync-action-pins)          |
   | **Workflows**         | Read and Write          | Push changes to `.github/workflows/` files — not available via YAML `permissions:` at all |

5. Click **Generate token** and copy it.

> [!TIP]
> **Token expiration**: Fine-grained PATs expire. Set a calendar reminder to rotate the token, or workflows will fail silently.

Run this command and paste the token when prompted:

```shell
gh secret set REPOMATIC_PAT --repo $repo_slug
```

Or add it manually: **this repo → [Settings → Secrets → Actions]($repo_url/settings/secrets/actions)** → **New repository secret** → name it `REPOMATIC_PAT` → paste the token.
