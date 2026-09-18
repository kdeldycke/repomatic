# {octicon}`book` Man pages

Every `repomatic` command carries a Unix manual page, walked out of the live command tree while this site builds. A manual therefore cannot describe a release other than the one it ships beside, which is the whole reason none of them is written by hand.

The index below is rendered by `click_extra.sphinx` from the `click_extra_manpages` list in `docs/conf.py`. Each entry points at the browser-viewable HTML rendering produced when `mandoc` (preferred) or `groff` is on `PATH` during the build.

```{click-extra-manpages}
```

The raw `.1` files sit in this same directory, next to the renderings above, under `repomatic.1` for the root command and `repomatic-{subcommand}.1` for the rest. [Reading them in a terminal](install.md#man-pages) covers the rest: `repomatic --man` for the root manual, and installing the release bundle into `MANPATH` to read any other with `man repomatic-{subcommand}`.
