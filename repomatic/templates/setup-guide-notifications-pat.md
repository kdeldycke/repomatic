---
args: [repo_url, repo_slug]
footer: 'false'
---

The [unsubscribe workflow]($repo_url/actions/workflows/unsubscribe.yaml) cleans up notification threads of closed issues and PRs. It needs its own token: the GitHub notifications API only accepts **classic** personal access tokens with the `notifications` scope (fine-grained tokens are rejected), so the fine-grained `REPOMATIC_PAT` cannot be reused.

1. Open the [**pre-filled classic token form**](https://github.com/settings/tokens/new?description=REPOMATIC_NOTIFICATIONS_PAT&scopes=notifications) (or go to **GitHub → Settings → Developer Settings → [Tokens (classic)](https://github.com/settings/tokens)** and click **Generate new token (classic)**).

2. Check that only the `notifications` scope is selected, set an expiration, and click **Generate token**.

3. Add it as a repository secret:

```shell
gh secret set REPOMATIC_NOTIFICATIONS_PAT --repo $repo_slug
```

Or add it manually: **this repo → [Settings → Secrets → Actions]($repo_url/settings/secrets/actions)** → **New repository secret** → name it `REPOMATIC_NOTIFICATIONS_PAT` → paste the token.

> [!NOTE]
> The weekly unsubscribe run skips silently until the secret is configured. Notifications are account-wide, so one repository with the secret cleans the whole inbox: there is no need to configure it in every repo that enables the workflow.
