---
args: [event_name, actor, rerun_row, ref_name, sha_short, commit_url, job, workflow_file, workflow_url, run_number, run_attempt, run_url]
---

<details><summary><code>Workflow metadata</code></summary>

| Field        | Value                                   |
| :----------- | :-------------------------------------- |
| **Trigger**  | `\$event_name`                          |
| **Actor**    | @\$actor                                |
| \$rerun_row  | **Ref**                                 |
| **Commit**   | [`\$sha_short`]($commit_url)            |
| **Job**      | `\$job`                                 |
| **Workflow** | [`\$workflow_file`]($workflow_url)      |
| **Run**      | [#\$run_number.\$run_attempt]($run_url) |

</details>
