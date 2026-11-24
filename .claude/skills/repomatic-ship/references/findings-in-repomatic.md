# Shipping the findings when the repo is repomatic

**Inside `kdeldycke/repomatic` there is no `../repomatic`, and the step 8 boundary inverts.** The canonical repo is both the subject of the release and the home of every skill, workflow and convention these findings target, so "propose it upstream" collapses into "fix it here". Leaving the diff uncommitted there buys nothing and costs the release: the next cycle ships the same rough edge, and the diff sits in the tree collecting conflicts against whatever the machinery rewrites meanwhile.

The pass stays in step 8, after babysit: the findings worth shipping mostly do not exist until babysit has surfaced what CI does and the local gate cannot.

Treat the findings commit as a reconciliation like any other, which means four things the uncommitted-diff path never had to handle:

- **Verify it with the same gate.** Run the part of step 2 the change touches: the pinned `<cmd> run mdformat` over an edited Markdown asset, `pytest tests/test_claude_assets.py tests/test_skills.py` over a bundled skill or agent, the full gate over Python. Bundled assets carry conformance tests that a docs page does not, and that difference decides how much you run.
- **Give it a changelog bullet only when a user can observe it.** A bundled skill, agent or workflow deploys verbatim to every downstream repo, so correcting one is a shipped fix and earns an entry. A change to this skill's own release choreography is not something a downstream user consumes, and earns none. When you do add one, re-run consolidation and present the diff (step 4) before committing.
- **Time the push by what it rebuilds** (step 6): a docs-only commit skips both matrices, and a `.claude/`-only one skips the binary matrix but runs Tests, since the test suite reads every bundled skill and agent.
- **Re-verify after it lands.** The push starts a fresh round of CI and re-bases the PR, so every green you read before it is stale. Confirm each monitored workflow is green on the new HEAD and that `<cmd> ci-status --release-pr` reports the PR `0` commits behind `main` again before step 7, and never report a release green from a run that predates your own last commit.

**A pre-existing defect that a lane reports is a finding too.** Step 1 has each lane report a defect older than the cycle, so that the reconciliation itself stays behavior-preserving. Fix each such defect here, before the step-7 report, as release work like any other finding. Give it a test that fails on the old code. List for the maintainer only the defects that the bound below keeps out.

**Keep the pass bounded to what the session actually surfaced.** A release is the wrong moment to land a refactor, so a finding needing more than a contained edit, or touching code the release itself depends on, stays an uncommitted diff plus a note in the step-7 report, exactly as the downstream path prescribes. The usable test is whether the gate you just ran can verify it; when it cannot, it is not a release-time change.
