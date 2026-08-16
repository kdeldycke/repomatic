# {octicon}`cloud` Cloudflare Pages

Setting `[tool.repomatic] site.deploy = "cloudflare-pages"` moves a repository's published site from GitHub Pages to a [Cloudflare Pages](https://developers.cloudflare.com/pages/) project. The reason to do it is the edge rather than the upload: a Cloudflare Pages custom domain carries its own certificate, so a zone's apex can stay proxied, which is what a `_redirects` file, a real `404.html` and any edge rule on the apex all depend on. This page is the operating manual for that hosting model: how a deploy works, how the credential is scoped and rotated, what drifts server-side, and how the `_redirects` engine really reads its file.

Everything here was learned by operating real Pages projects, the expensive way where noted. The negative results are kept on purpose: an endpoint that refuses a token type or a setting that looked alarming and was not are exactly the findings most likely to be rediscovered at full price.

## Direct Upload, and why nothing else

The site is never built by Cloudflare. CI renders the tree and uploads the finished files with `wrangler pages deploy`, so Cloudflare needs no access to the repository and runs no build of its own. In the API, those deployments carry `deployment_trigger.type = "ad_hoc"`, and the one currently serving is the project's `canonical_deployment`. A deployment only activates once its upload completes, so an interrupted run leaves the previous one serving: cancelling a superseded deploy is always safe.

The project's `source` must read `null`, and must stay that way. Attaching a git repository reintroduces a second, competing publisher for the same project, one with no build configuration capable of producing a usable site. The [drift check](#the-drift-check) fails when a source block appears, which is the guard against it coming back through a well-meaning dashboard visit.

Two platform limits shape the upload. Direct Upload rejects any file over 25 MiB, and `wrangler` fails the whole deploy on the first one it meets, so the deploy job drops oversized files first and names each one in the log: everything else publishes instead of nothing. And each project keeps its `<project>.pages.dev` hostname for life; see [below](#the-pagesdev-hostname) for why that is fine.

## The token

`CLOUDFLARE_ACCOUNT_ID` is not a credential: it is a stable identifier visible in every dashboard URL, it never expires, and it never needs rotating. `CLOUDFLARE_API_TOKEN` is the credential, and it needs exactly one permission: **Account → Cloudflare Pages → Edit**. Nothing else.

Create it account-owned, with Cloudflare's [account token template URL](https://developers.cloudflare.com/fundamentals/api/how-to/account-owned-token-template/): the [pre-filled form](https://dash.cloudflare.com/?to=/:account/api-tokens&permissionGroupKeys=%5B%7B%22key%22%3A%22page%22%2C%22type%22%3A%22edit%22%7D%5D) arrives with the permission already selected. Two things the URL cannot carry stay manual: a name that says what the token is and when it was made (like `my-project-deploy-2026-08`), and a one-year TTL. Avoid the tokens under **My Profile → API Tokens**: those are user-owned, die with the user, and their manual creation form defaults its permission dropdown to *User*, where Cloudflare Pages does not even appear.

One trap is worth writing down because it looks exactly like a broken credential. `GET /user/tokens/verify` is user-scoped, so an account-owned token (the `cfat_` prefix) answers **HTTP 401 "Invalid API Token"** there while every project call succeeds. A verify step is therefore the wrong thing to gate a script on: prove the credential against the project it is meant to touch instead, which is what every mode of `repomatic cloudflare-pages` does implicitly.

### The token cannot be scoped to one project

It is the obvious next question, and the answer is no. `Cloudflare Pages` is an [account permission](https://developers.cloudflare.com/fundamentals/api/reference/permissions/), whose applicable scope is `com.cloudflare.api.account`, so the resource a token restricts to is an *account*. There is no per-project selector and no per-domain one: a deploy token for one site can edit **every** Pages project on the account, delete them, or replace what any of them serves.

Two consequences worth acting on rather than filing away. The blast radius of a leaked deploy token is every site on the account, not the one repository holding it, which is what makes the one-year TTL and the rotation habit load-bearing rather than ceremonial. And the account is the only isolation boundary on offer, so sites that genuinely must not be able to touch each other need separate accounts; nothing inside a single account will separate them.

The narrowest workable deploy credential is therefore exactly what the setup guide asks for: `Cloudflare Pages → Edit`, on one account, with an expiry. Adding `Account → Cloudflare Pages → Read` alongside it buys nothing, since Edit implies it, and adding anything else widens a credential that CI keeps for a year.

The TTL bounds the damage of a leak, and on its own it would create a silent failure: a push-only deploy exercises the token whenever the next change happens to land, which for a quiet repository is never. Cloudflare sends no expiry warning for API tokens (the "expiring token" notification in its docs covers Access service tokens, a different product). The Docs workflow closes that gap twice over: its monthly run turns a lapsed token into a red run and an email, and the [drift check](#the-drift-check) starts warning a month before expiry.

### Rotating the token

Create the replacement before revoking the incumbent, so no window exists where a push cannot deploy.

1. Create the new token as above, with a fresh one-year TTL and a name carrying the new date.
2. Update the secret without the value entering a shell history or a terminal transcript, straight from the clipboard:
   ```shell
   pbpaste | gh secret set CLOUDFLARE_API_TOKEN --repo owner/repo
   ```
3. Verify with a real run before revoking anything: dispatch the Docs workflow and watch the deploy step complete. A too-narrow scope fails there, and the previous deployment keeps serving.
4. Only then delete the old token.

## The drift check

Everything that actually shapes the live project (the compatibility date, Smart Placement, the build image, whether a source got attached) lives server-side in `deployment_configs`, invisible to anyone reading the repository. Those settings are inert until they suddenly are not, which is how one project's compatibility date sat three years behind the live value with nothing noticing. `repomatic cloudflare-pages` makes that state explicit, diffable and re-applicable:

```shell
repomatic cloudflare-pages --check    # diff live against declared, exit 1 on drift
repomatic cloudflare-pages --apply    # write the declared values back
repomatic cloudflare-pages --dump     # full live state, secrets redacted
repomatic cloudflare-pages --create   # create the project, then configure it
```

The declarations live beside the target in `[tool.repomatic]`: `site.cloudflare-compatibility-date` and `site.cloudflare-placement`, each unmanaged while unset. Two invariants are always checked but never written, because getting them wrong breaks publishing rather than aging it: the `source` block must stay `null` and the build command must stay empty, both consequences of Direct Upload. The build image major version is asserted at `3`, the terminal version Cloudflare auto-migrates old projects onto.

The diff is honest about its own confidence. Each stock default is tagged `documented` when quoted from Cloudflare's documentation or `inferred, unverified` when deduced from product behaviour, so a later reader can tell a verified fact from a plausible assumption instead of watching one launder into the other.

`wrangler.toml` is not part of the server-side state, and that is precisely its hazard: Cloudflare honours the project's own configuration, while the file only matters to local `wrangler` commands on a project that is never built remotely. It can therefore lie for years. `lint-repo` compares its `name` and `compatibility_date` against the declared values, so the repository states each fact once.

`--create` covers the rebuild-from-nothing case: the Pages API creates the Direct Upload project (something `wrangler pages deploy` refuses to do non-interactively), then the declared settings are applied to it. With the two secrets restored and a push to the default branch, the whole hosting side reconstructs from what is committed.

## The redirects engine, as it actually is

Cloudflare's [documentation](https://developers.cloudflare.com/pages/configuration/redirects/) describes the `_redirects` file format. The behaviour that decides whether rules *exist* is only in the implementation: `parseRedirects.ts` and `rules-engine.ts` in [cloudflare/workers-sdk](https://github.com/cloudflare/workers-sdk), the code wrangler and Miniflare bundle. {mod}`repomatic.pages_redirects` is a faithful Python replica, transcribed 2026-08-10, and `lint-repo` runs every committed `_redirects` file through it.

**The accounting.** A rule counts as *static* (budget: 2000) only while it appears **before the first rule containing `*` or `:placeholder`**. From that rule on, every line, however static it looks, burns the *dynamic* budget of 100. At rule 101 of that mixed stream the parser does not skip a line, it **stops reading the file**. Everything after is silently discarded: `wrangler pages deploy` prints nothing about it, and a dead redirect looks exactly like a URL nobody visits. One site interleaved statics and dynamics by theme and lost its last 18 rules for years this way. The fix is always the same reorder, all exact rules first, all pattern rules second, and it is behaviour-preserving because the runtime probes exact sources ahead of patterns wherever they sit in the file.

**The matching.** Sources compile to anchored regexes: `:name` becomes `[^/]+` (at least one character, never a slash, never empty), `*` becomes `.*` (may be empty), the whole source is wrapped `^…$`. Consequences: `/a` and `/a/` are different sources that never match each other, and a rule ending in `/*` also answers the bare trailing-slash URL through an empty splat, which is what keeps the oldest links alive on sites whose historical URLs all ended in a slash.

**Parse details worth knowing.** Inline comments after a rule are stripped; duplicate sources are dropped silently, first one wins; statuses are limited to 200, 301, 302 (the default), 303, 307 and 308, where 200 proxies a relative path instead of redirecting; a rule whose relative destination ends in `/index` or `/index.html` is refused as an infinite loop with the engine's own `.html`-stripping; lines over 2000 characters are ignored.

**What the file cannot do.** Pages redirects match on **path only, never on host**. Host canonicalization (an apex or `www` variant redirecting to the canonical hostname) has to happen at the zone edge, in a Single Redirect rule, before Pages sees the request: a `/*` rule in `_redirects` would fire on the canonical hostname too and redirect the site to itself forever. Account-level Bulk Redirects are the other neighbour, essentially static (no placeholders, no splats), and only worth splitting the inventory over if the dynamic budget ever actually tightens.

## Leaving GitHub Pages behind, URLs intact

A repository moving from `site.deploy = "github-pages"` to `cloudflare-pages` leaves every URL already published under `https://<owner>.github.io/<repo>/…` pointing at the old host: search indexes, other projects' readmes, years of issue comments, and the `[project.urls]` metadata frozen into every release already on PyPI. None of those can be rewritten, so the old host has to keep answering.

It does, for free, and the whole mechanism is one field. Set the repository's GitHub Pages **custom domain** to the new hostname, and GitHub answers `https://<owner>.github.io/<repo>/…` with a real `301` to `https://<domain>/…`, preserving the path. It applies to every path, including ones the site never had, so nothing needs enumerating: no stub files, no `sitemap.xml` to mine, no catch-all `404.html`, and nothing to keep in sync as the docs grow. Set it with `gh api --method PUT repos/{owner}/{repo}/pages --field cname=<domain>`, or in **Settings → Pages → Custom domain**.

GitHub issues that redirect even though the domain resolves to Cloudflare rather than to GitHub, because the response comes from the `*.github.io` hostname and its certificate; the custom domain is only ever named in the `Location` header. The Pages settings screen may still complain that the domain does not resolve to GitHub, or fail to provision a certificate for it. Both are cosmetic here: neither is on the path of the redirect.

Three things keep it working:

- **Leave GitHub Pages enabled.** Disabling it removes the redirect and every historical link dies at once. The last deployment's *content* stops being reachable the moment the custom domain is set, since every request to it redirects away, but the Pages site itself has to stay.
- **Let the deploy job stop.** With `site.deploy` naming Cloudflare, the GitHub Pages job is gated off, so the unreachable copy simply stops being rebuilt. That is the intended end state, not an oversight.
- **Point `html_baseurl` at the new domain** in `docs/conf.py`, so Sphinx emits `<link rel="canonical">` naming the host that now serves the page. A site with no `html_baseurl` emits no canonical tag at all, which is the one gap the redirect cannot cover.

A `sphinx.builder` switch from `html` to `dirhtml` can ride along without adding a single rule. Cloudflare Pages resolves `/page.html` to the `page/index.html` that `dirhtml` writes and `301`s to `/page/` on its own, so an old `…github.io/<repo>/install.html` link lands on `<domain>/install/` in two hops, each a permanent redirect. Neither hop is anything you maintain.

What is left is the repository advertising the old host itself: the GitHub homepage field, `[project.urls]`, badges, and every absolute self-link in the docs and in the PR and issue templates that render into other repositories. `lint-repo`'s website check catches a half-finished job by comparing the homepage field against the declared documentation URL. Once traffic lives on Cloudflare, any *further* URL move is the [redirects engine's](#the-redirects-engine-as-it-actually-is) job: GitHub's redirect answers for the old host, `_redirects` for the new one, and neither can do the other's work.

## Headers and content types

Pages guesses content types far better than its documentation suggests: mainstream and niche extensions alike arrive correctly typed without any rule. A `_headers` file should therefore carry only the measured exceptions, each rule existing because a live response was observed wrong, not because it might be. The same principle catches the opposite mistake: a format with no registered media type is honestly `application/octet-stream`, and "fixing" it would be the regression.

Pages also sends `X-Content-Type-Options: nosniff` on every response as a platform default. Pinning that in `_headers` with a `/*` rule costs one line and makes the guarantee the site's own, so a host migration or a changed platform default cannot silently drop it.

Like `_redirects`, the file matches on path only, and it only means anything once Cloudflare serves it: nothing in a build proves an edge header works, so a suite that fetches the live site is the only real check.

## The pages.dev hostname

Every Pages project gets `<project>.pages.dev`, keeps it for the life of the project, and cannot delete it: it is the CNAME target a custom domain resolves through, so the site is served *via* that hostname. Renaming the project moves it, which is why `site.cloudflare-project` exists for projects that predate repomatic.

The duplicate copy it exposes is inert as long as every generated page carries a `<link rel="canonical">` naming the custom domain and the sitemap lists canonical URLs only: the copy goes unindexed, and a crawler landing there leaves on the first click. Actually redirecting `pages.dev` to the custom domain would need host matching, which neither `_redirects` nor `_headers` can express, so it would take a Pages Function running on every request of an otherwise fully static site. Not worth it while the copy stays out of the index.

## Reading the audit log

**Manage account → Audit logs** answers "what is using this account". The **Actor Context** column is the one that matters:

| Actor Context             | Means                                               |
| ------------------------- | --------------------------------------------------- |
| `dash`                    | A human in a browser                                |
| `api`                     | Dashboard-driven API calls: `LOGIN`, `TOKEN_CREATE` |
| `api_token`               | A scoped API token                                  |
| *(empty)*, actor `system` | Cloudflare itself, mostly certificate renewal       |

Account-owned and user-owned tokens are distinguishable after the fact: an account-owned token logs actor `account` with an empty Actor Email, a user-owned one logs the owner's address. That is how a credential migration gets confirmed rather than assumed.

Certificate renewal is the noise to learn once and never investigate again. Cloudflare issues and renews the certificates for Pages custom domains and Universal SSL entirely internally: it controls the DNS and the edge, so no API token is involved at any point. In the log this shows up several times a month as a repeating `Certificate pack created` / `Certificates ordered` / `Certificate pack deployed` cycle, each bracketed by DNS record create and delete pairs (the validation records Cloudflare writes to prove domain control and then cleans up). Every one of those entries has actor `system` and an empty Actor Context. It is not DNS drift and it is not anything of yours.

Two limits worth knowing before drawing conclusions from the log: it records **changes only**, so a credential that only ever reads leaves no trace however far back anyone looks, and reading it through the API needs `Account → Audit Logs → Read`, which deploy-scoped tokens deliberately do not carry.
