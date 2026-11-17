---
args: [previous, adopted, command]
footer: false
---

## 🧭 Upgrade review

This pull request moves `repomatic` from `\$previous` to `\$adopted`. Once it is merged, pull the default branch and run the [`/repomatic-upgrade`](https://repomatic.net/agent-skills) skill in a fresh session. It applies what the new release lets this repository adopt, reuse or drop, audits the drift from the new tag, then commits its changes locally and pushes nothing:

```shell
$command
```
