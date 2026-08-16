---
args: [repo_url, repo_slug]
footer: 'false'
---

The [unsubscribe workflow]($repo_url/actions/workflows/unsubscribe.yaml) needs its own **classic** token: the notifications API rejects fine-grained ones, so `REPOMATIC_PAT` cannot be reused. It skips silently until this is set.

1. Open the [**pre-filled classic token form**](https://github.com/settings/tokens/new?description=REPOMATIC_NOTIFICATIONS_PAT&scopes=notifications), which arrives with only the `notifications` scope selected.

2. Set an expiration, then click **Generate token**.

3. Add it as a repository secret:

   ```shell
   gh secret set REPOMATIC_NOTIFICATIONS_PAT --repo $repo_slug
   ```

   Or by hand: **[Settings → Secrets → Actions]($repo_url/settings/secrets/actions)** → **New repository secret** → `REPOMATIC_NOTIFICATIONS_PAT`.

> [!NOTE]
> Notifications are account-wide, so one repository holding this secret cleans the whole inbox. There is no need to repeat it everywhere the workflow is enabled.
