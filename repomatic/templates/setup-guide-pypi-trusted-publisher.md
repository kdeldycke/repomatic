---
args: [package_name, repo_owner, repo_name, workflow_filename, settings_url]
footer: 'false'
---

Register a **Trusted Publisher** on PyPI so `publish-pypi` uploads through OIDC.

1. Open the [**pre-filled form**]($settings_url). If `$package_name` is not on PyPI yet, open the [account settings](https://pypi.org/manage/account/publishing/) instead, to register a **pending publisher** with **PyPI Project Name** set to `$package_name`.

2. Make the fields match these values, then click **Add**:

   | Field                | Value                |
   | :------------------- | :------------------- |
   | **Owner**            | `$repo_owner`        |
   | **Repository name**  | `$repo_name`         |
   | **Workflow name**    | `$workflow_filename` |
   | **Environment name** | *(leave blank)*      |

   > ⚠️ **Warning**: The workflow name is this repository's own `$workflow_filename`, never the upstream reusable workflow path, or the first publish fails ([pypi/warehouse#11096](https://github.com/pypi/warehouse/issues/11096)).
