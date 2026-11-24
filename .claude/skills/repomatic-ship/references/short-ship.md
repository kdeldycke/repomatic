# Repairing a short ship

When the binary matrix came up short, say so in the step 7 report and name the platforms the version will lose.

Name the missing platforms nowhere else. The changelog section and the GitHub release body describe the version, not each of its artifacts, so neither takes a `> [!WARNING]` about a missing binary.

One artifact still links binaries that are not there, and needs a hand *after* the merge: **`docs/install.md`**. In repomatic itself, the freeze pins its download URLs to the version being released, optimistically: the freeze commit is what triggers the build, so it cannot know whether the binaries will land. Re-point them at the last release that carries binaries via `PrepareRelease.freeze_install_download_urls`, and the next release's freeze ratchets them forward again. The freeze leaves a downstream repo's URLs alone. Those use the `latest/download/` form, which follows the newest release, so they 404 for the missing platforms until a later release ships them. There, the hand is a line in the report naming that gap, and no file changes. `lint-repo`'s `check_install_guide_downloads` reports the gap in both cases but never repairs it, since an automated rewrite driven by a single API read could downgrade a healthy install page on a flaky response.

Report it as a follow-up. Do not perform it: it lands after a merge this skill does not make.
