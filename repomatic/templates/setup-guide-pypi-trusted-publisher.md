---
args: [package_name, repo_owner, repo_name, workflow_filename, settings_url]
footer: 'false'
---

Register a [**Trusted Publisher**]($settings_url) entry on PyPI so the `publish-pypi` job can upload releases via OIDC, with no long-lived API token.

The publisher matches the OIDC `job_workflow_ref` claim, which names the **caller's** workflow file (`.github/workflows/$workflow_filename` in this repo). Without a matching entry, the first upload after migration fails with a publisher-mismatch error.

Open the [**pre-filled Trusted Publishers settings page**]($settings_url): the GitHub tab is already selected and every required field is populated. Review the values and click **Add**:

| Field                | Value                |
| :------------------- | :------------------- |
| **Owner**            | `$repo_owner`        |
| **Repository name**  | `$repo_name`         |
| **Workflow name**    | `$workflow_filename` |
| **Environment name** | *(leave blank)*      |

> [!NOTE]
> The workflow name must be `release.yaml`, not the upstream reusable workflow path. The composite action invoked from `release.yaml` inherits the caller's OIDC context, so the claim resolves to this repo's own file. Registering the upstream path triggers [pypi/warehouse#11096](https://github.com/pypi/warehouse/issues/11096) on first publish.

If the project does not yet exist on PyPI, register a **pending publisher** instead from the [account-level settings](https://pypi.org/manage/account/publishing/), using the same field values plus a **PyPI Project Name** field set to `$package_name`. PyPI promotes it to a regular publisher on the first successful upload.

See the [PyPI Trusted Publishers documentation](https://docs.pypi.org/trusted-publishers/adding-a-publisher/) for the full registration walkthrough.
