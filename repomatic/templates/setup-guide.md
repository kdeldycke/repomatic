Some workflows need a **fine-grained personal access token** to create PRs that update files in `.github/workflows/`. Without it, those jobs will silently fail.

### Step 1: Create the token

1. Go to **GitHub → Settings → Developer Settings → [Fine-grained tokens](https://github.com/settings/personal-access-tokens)**.

2. Click **Generate new token**.

3. Under **Repository access**, select this repository.

4. Add these permissions:

   | Permission            | Access                  | Reason                                                                                    |
   | :-------------------- | :---------------------- | :---------------------------------------------------------------------------------------- |
   | **Commit statuses**   | Read and Write          | Renovate `stability-days` status checks                                                   |
   | **Contents**          | Read and Write          | Tag pushes, release publishing, PR branch creation                                        |
   | **Dependabot alerts** | Read-only               | Renovate reads vulnerability alerts to create security PRs                                |
   | **Issues**            | Read and Write          | Renovate [Dependency Dashboard](https://docs.renovatebot.com/key-concepts/dashboard/)     |
   | **Metadata**          | Read-only *(mandatory)* | Required for all fine-grained token API operations                                        |
   | **Pull requests**     | Read and Write          | All PR-creating jobs (sync-workflows, fix-typos, prepare-release, Renovate)               |
   | **Workflows**         | Read and Write          | Push changes to `.github/workflows/` files — not available via YAML `permissions:` at all |

5. Click **Generate token** and copy it.

### Step 2: Add the secret

1. Go to **this repo → [Settings → Secrets → Actions]($%7Brepo_url%7D/settings/secrets/actions)**.
2. Click **New repository secret**.
3. Name: `WORKFLOW_UPDATE_GITHUB_PAT`
4. Paste the token and click **Add secret**.

### Step 3: Configure Dependabot settings

Go to **this repo → [Settings → Advanced Security → Dependabot]($%7Brepo_url%7D/settings/security_analysis)** and configure:

| Setting                         | Status      | Reason                                                |
| :------------------------------ | :---------- | :---------------------------------------------------- |
| **Dependabot alerts**           | ✅ Enabled  | Renovate reads these alerts to detect vulnerabilities |
| **Dependabot security updates** | ❌ Disabled | Renovate creates security PRs instead                 |
| **Grouped security updates**    | ❌ Disabled | Not needed when security updates are disabled         |
| **Dependabot version updates**  | ❌ Disabled | Renovate handles all version updates                  |

> [!WARNING]
> Keep **Dependabot alerts** enabled — Renovate reads them via the API. Disable all other Dependabot features.

### Step 4: Enable immutable releases

Go to **this repo → [Settings → General]($%7Brepo_url%7D/settings)**, scroll to the **Releases** section, and enable **Release immutability**.

This locks git tags and release assets after publication, preventing tampering. Release notes remain editable.

> [!WARNING]
> **Permanent tag reservation**: tags used for immutable releases are permanently reserved, even after deleting the release. If a release goes wrong, skip to the next version — don't try to reuse the tag.

### Step 5: Verify

Re-run the workflow. Jobs should now update `.github/workflows/` files without errors.

> [!WARNING]
> **Token expiration**: Fine-grained PATs expire. Set a calendar reminder to rotate the token, or workflows will fail silently.

\$org_tip

This issue will close automatically once the secret is detected.

---

🏭 Generated with [repomatic](https://github.com/kdeldycke/repomatic)
