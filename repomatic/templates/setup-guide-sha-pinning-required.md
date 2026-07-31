---
args: [repo_url, repo_slug]
footer: 'false'
---

Require every GitHub Action to be pinned to a full-length commit SHA, enforced by GitHub itself. repomatic already pins every action it generates and checks unpinned refs with `zizmor`, but a `zizmor` finding can be silenced inline (`# zizmor: ignore[...]`), so a hand-edited workflow could still slip a mutable tag past review. This setting closes that gap: GitHub refuses to run any workflow referencing an action by tag or branch instead of a commit SHA.

`enabled` is a required field on this endpoint, so the fix reads the current settings and writes them back with only `sha_pinning_required` flipped, leaving `allowed_actions` untouched:

```shell
gh api repos/$repo_slug/actions/permissions --jq '. + {sha_pinning_required: true}' \
  | gh api --method PUT repos/$repo_slug/actions/permissions --input -
```

Or set it manually at [**Settings → Actions → General**]($repo_url/settings/actions) → **Actions permissions** → **Require actions to be pinned to a full-length commit SHA**.

> [!NOTE]
> Reusable workflows (`uses: owner/repo/.github/workflows/x.yaml@ref`) can still be referenced by a tag even with this setting on: it only enforces pinning for actions.
