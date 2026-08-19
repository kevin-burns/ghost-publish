---
name: ghost-publish
description: Publish, update, schedule and verify posts on a Ghost blog from a markdown file, driving the official `ghst` CLI. Use when the user wants to push a draft to Ghost, update an existing post, schedule a post for a future date, set tags/slug/excerpt/feature image, or check that what Ghost actually holds matches the source file. Also use when a Ghost upload has gone wrong — front matter showing in the post body, a stale draft, missing tags, a post that reads differently on the site than in the file. Covers the traps that bite every first run: `--markdown-file` sends YAML front matter as visible text, `post update` converts markdown to Lexical so the source is not recoverable, `post get` returns no `html` field, and `auth login` fails behind any authenticating proxy. Not for writing or editing the prose itself — use clear-and-human for that.
license: MIT
---

# Ghost Publish

Drives [`ghst`](https://github.com/TryGhost/ghst), Ghost's official CLI, to get a markdown
file onto a Ghost blog and then **prove** that what arrived is what you sent.

The proving is the point. Ghost's import is lossy in ways that are invisible from the CLI's
own output: a successful `post update` returns a clean JSON object while YAML front matter
sits in plain sight above the first paragraph of the published post.

## Boundary

This skill does **not** write, edit, or improve the prose. It moves an already-final file and
checks it. For the writing itself use `clear-and-human`; for a CV use `cv-and-human`.

It does not publish or schedule unless told to in the current turn. Scheduling with a
newsletter attached sends email to real subscribers at the scheduled moment with no second
confirmation — treat it as irreversible and outward-facing.

It does not configure Ghost, Docker, DNS, or any reverse proxy. If `ghst` cannot reach the
site, that is the user's infrastructure to fix; see [Connection](#connection) for how to tell
that apart from a skill problem.

## Connection

> **If your blog sits behind an identity provider — Cloudflare Access, Tailscale, Authelia,
> an SSO proxy — `ghst` cannot get past it, and neither can this skill.** That is not a defect
> in either. The IdP is doing exactly its job: refusing an unauthenticated request. Resolving
> it is the operator's side of the line, and this section says how. Do not report it as a skill
> bug, and do not work around it by weakening the IdP until you have tried the routes below —
> the usual answer costs one header and changes no security posture at all.

**Assume nothing about auth. Test it first, in one call:**

```bash
ghst post list --json --jq '.posts[0].title'
```

A title means you are connected and can skip the rest of this section. Anything else is a
setup problem the user has to resolve before this skill can do anything.

Two things make that test fail in ways worth naming, because both look like CLI bugs:

**`ghst auth login` fails behind an authenticating proxy.** Cloudflare Access, any Zero Trust
IdP, or an SSO proxy answers the discovery request with a redirect to its login host. `ghst`
reads that as the site's real origin and aborts with `USAGE_ERROR: Ghost Admin discovery
resolved to '<idp-host>' instead of '<your-host>'`. **Do not follow its advice to re-run with
the IdP's URL** — that points the CLI at a login page, not a Ghost site. Skip `auth login`
entirely and use environment variables, which sit third in `ghst`'s connection-resolution
order, ahead of the config files that `auth login` would have written:

```bash
export GHOST_URL="https://ghost.example.internal"
export GHOST_STAFF_ACCESS_TOKEN="{id}:{secret}"
```

**Ghost 301-redirects to its own configured `url` when it thinks the connection is
insecure.** A Ghost that serves `https://blog.example.com` but listens on plain HTTP behind a
proxy will bounce a direct internal request straight back out to the public hostname — and
into whatever gateway you were trying to avoid. The fix is one request header, `X-Forwarded-Proto:
https`, which is what the reverse proxy in front of it already sets on the public path. `ghst`
has no flag for sending headers, so this belongs in a proxy rule rather than in the CLI, e.g.
in Caddy:

```
reverse_proxy ghost-host:2368 {
        header_up X-Forwarded-Proto https
}
```

The explicit `header_up` is required, not cosmetic: Caddy derives `X-Forwarded-Proto` from the
incoming scheme, which is `http` on an internal hop, reproducing the redirect.

Diagnose which of the two you have with one call each — no auth needed:

```bash
curl -s -o /dev/null -w '%{http_code} -> %{redirect_url}\n' https://your-blog/ghost/api/admin/site/
curl -s -o /dev/null -w '%{http_code}\n' -H 'X-Forwarded-Proto: https' http://internal-host:2368/ghost/api/admin/site/
```

A 302 to an IdP is the first problem. A 301 to your own public URL that becomes 200 with the
header is the second.

## Publishing a file

**Never send the source file directly.** `--markdown-file` transmits bytes verbatim, and
Ghost has no concept of front matter — unlike Hugo or Jekyll, a leading `---` block renders as
body text. Strip it first, and pass what it contained as real Ghost fields:

```bash
uv run ~/.claude/skills/ghost-publish/scripts/prepare_post.py post.md --out /tmp/body.md
```

It writes the body without front matter and prints the `ghst` flags the front matter implies.
Then create or update:

```bash
# new post -- note there is NO --slug on `post create`
ghst post create --title "..." --markdown-file /tmp/body.md \
  --tags "Writing,Python" --json --jq '.posts[0].id'

# existing post
ghst post update <post-id> --markdown-file /tmp/body.md --tags "Writing,Python" --json
```

**A post's slug is not settable by flag.** `post create` has no `--slug`, and `post update
--slug` is a *lookup* that selects a post rather than renaming one. The only route is a JSON
payload, which `prepare_post.py --payload` writes for you:

```bash
uv run scripts/prepare_post.py post.md --out /tmp/body.md --payload /tmp/post.json
ghst post create --from-json /tmp/post.json --markdown-file /tmp/body.md --json
```

Pages are the exception and take the flag directly — `page create --slug my-page` sets it. Pass
`--target page` to `prepare_post.py` and it renders `--slug` among the flags. Verified on 0.16.6.

`--tags` replaces the tag set rather than adding to it, so pass the full list every time.
Slug, excerpt and feature image survive an update untouched — but verify rather than trust,
because that is a behaviour, not a guarantee.

### Updating a post that is already live

Measured on 0.16.6, 2026-08-19, against a published post rather than a draft. All four of
these are behaviours rather than promises, so re-check them after an upgrade:

| what | what happened |
|---|---|
| `status` | stayed `published`. The update does not knock a live post back to draft. |
| `published_at` | **unchanged.** The edit landed in `updated_at` only. |
| RSS `pubDate` | unchanged, so the post does not jump the feed or re-notify readers. |
| email | none. Email fires on the publish transition, and an update is not one. |
| slug, tags, excerpt, feature image | all survived. |

`published_at` holding still is the one worth knowing, because the alternative is loud: a
post that re-dates on every typo fix climbs back to the top of the feed and reaches everyone
subscribed to it again.

**There is no staging step.** `post update --markdown-file` rebuilds the whole document, so a
live post is briefly whatever you just sent while you are still finding out whether it was
right. Run `verify_post.py` immediately after, not at the end of the session, and keep the
edit small enough that you would be comfortable with a reader seeing it mid-flight.

**The feature image is a separate upload.** `ghst image upload` returns a URL; setting it on
the post is its own step. This is also the one capability the MCP server does not have, which
is why this skill drives the CLI throughout.

## Cards

**Most of them need nothing.** Ghost's markdown conversion already produces native cards, so
write ordinary markdown and check the result:

| markdown | becomes |
|---|---|
| `![alt](url)` on its own line | `image` card |
| `> quote` | `extended-quote` |
| fenced code block | `codeblock`, language preserved |
| a markdown table | `html` card holding the rendered `<table>` |
| `---` | `horizontalrule` |
| a pasted provider `<iframe>` | `embed` card, markup preserved |

**Never put an image inline.** `![](url)` inside a sentence splits the paragraph into text,
image card, text — so the sentence arrives in two pieces around a picture. Images go on their
own line.

Two shapes markdown cannot express, because both are editor behaviours:

- **Galleries.** Consecutive images become separate image cards with a spacer paragraph
  between each, never a gallery. Ghost's limit is **nine images**, laid out three to a row.
- **Embeds from a bare URL.** A video link on its own line becomes a clickable link. The
  editor embeds on paste; the markdown converter does not.

`enrich_cards.py` rewrites both, on the document Ghost has already produced:

```bash
ghst post get <post-id> --json --jq '.posts[0].lexical' > /tmp/lexical.json

# 1. which URLs would become embeds?
uv run ~/.claude/skills/ghost-publish/scripts/enrich_cards.py /tmp/lexical.json --list-embeds

# 2. fetch each from Ghost's own oEmbed endpoint -- no third-party call.
#    Note `ghst api` rejects query strings in the path: use --query.
ghst api /oembed/ --query "url=<the-url>" --query "type=embed" --json

# 3. rewrite, then push the result back
uv run ~/.claude/skills/ghost-publish/scripts/enrich_cards.py /tmp/lexical.json \
  --images-dir ./images --oembed /tmp/oembed.json --out /tmp/enriched.json
ghst post update <post-id> --lexical-file /tmp/enriched.json
```

`--oembed` takes a JSON object mapping each URL to its payload. `--images-dir` is where the
local image files live, and it is **required for galleries**: image cards built from markdown
carry `width: null`, Ghost lays a gallery out by aspect ratio, and rather than emit a null the
script declines the merge and says which file it could not measure. Separate image cards look
ordinary; a broken gallery does not.

**The round trip is lossless, which is what makes any of this safe.** `post get` → transform →
`post update --lexical-file` returns every node byte-identical: verified by moving a gallery to
the end of a post and comparing fingerprints, where all seven nodes — an embed with its iframe
and full oEmbed metadata, an HTML card with its visibility block, a four-image gallery —
survived unchanged. So reordering cards, or rewriting one, will not quietly damage the rest.

`--lexical-file` wants the **document itself**, not a JSON-encoded string of it. Writing
`json.dumps(json.dumps(doc))` is an easy slip, and Ghost rejects it with a 422 `Validation
error, cannot edit post` and leaves the post untouched — a loud failure, not a silent one.

**Paste a provider's iframe into the markdown, not into an HTML card.** The two land in
different Ghost nodes and they are styled differently. A raw `<iframe>` in a markdown file
becomes an `embed` node, rendered as `<figure class="kg-card kg-embed-card">`, and themes
generally centre that — the default one uses `align-items:center; display:flex;
flex-direction:column`. The editor's HTML card becomes an `html` node, `kg-html-card`, which
typically has **no layout rule at all**, so a fixed-width player sits left-aligned.

Three routes, three different results — measured on a rendered page rather than assumed:

| how the iframe gets in | node | rendered as | centred by a typical theme |
|---|---|---|---|
| the **markdown file**, uploaded by `ghst` | `embed` | `<figure class="kg-card kg-embed-card">` | **yes** |
| an **HTML card** in the editor | `html` | `<div class="kg-card kg-html-card">` | no |
| a **markdown card** in the editor | `markdown` | a bare `<iframe>`, **no wrapper at all** | no |

That third row is the one that surprises people, and it defeats the obvious fix: a markdown
card renders its raw HTML straight into the content flow with no card class, so there is no
`kg-` hook to style.

If hand-authored cards need centring too, that is a stylesheet fix rather than a content one —
a transform would have to be reapplied on every re-upload, since regenerating the post from
markdown rebuilds the card. Match the shape you actually have:

```css
.kg-html-card iframe   { display: block; margin-left: auto; margin-right: auto; }  /* HTML card */
.gh-content > iframe   { display: block; margin-left: auto; margin-right: auto; }  /* markdown card */
```

Target the iframe rather than the card. Markdown tables also become `html` cards, so centring
`.kg-html-card` itself would stop them filling the column.

**An embed card cannot be edited in Ghost's editor** — you can change its caption, but not its
URL. Changing what it points at means deleting the card and adding a new one. That is a real
constraint of the editor, and it pushes people toward pasting raw HTML into a markdown or HTML
card instead, which is how a player ends up off-centre.

**It is not a constraint of the content.** Rewriting an embed's `url` and `html` through
`--lexical-file` works, verified by changing one and reading it back. So on this skill's path
the editor's limitation never applies: **the markdown file is the source, you edit it there and
re-upload.** The embed being immutable in the UI costs nothing.

That does imply one rule, and it is the important one: **pick a single source of truth.** If the
file is authoritative, hand-edits made in the editor are overwritten by the next upload — not
just the embed, the whole document. If you are authoring in the editor, do not re-upload from
file. Mixing the two loses work in whichever direction you ran last.

**Not every provider is an oEmbed provider.** Bandcamp is not: `/oembed/` answers
`No provider found for supplied URL`. For those, paste the provider's own `<iframe>` into the
markdown — Ghost still makes an `embed` card from it, with `embedType` empty and `metadata` `{}`,
and the markup preserved.

**A provider's own embed code is left exactly as pasted.** Bandcamp, and anything else you
paste as a raw `<iframe>`, becomes an `embed` card whose `html` is the provider's markup —
width, styling and all. Ghost normalises the HTML syntax (`seamless` becomes `seamless=""`)
and changes nothing else: no width rewriting, no injected centering. This skill does not touch
it either, and a test pins that.

## Pages

Everything here works on a Ghost **page** as well as a post, because a page holds the same
Lexical document. Swap the noun in the command and nothing else changes:

```bash
ghst page create --title "..." --markdown-file /tmp/body.md --slug my-page --tags "..." --json
ghst page get <page-id> --json --jq '.pages[0].lexical' > /tmp/lexical.json
ghst page update <page-id> --lexical-file /tmp/enriched.json --json
```

Note the `.pages[0]` accessor rather than `.posts[0]` — that is the only shape difference, and
getting it wrong yields `null`, which reads as an empty document rather than as a bad query.
`prepare_post.py --target page`, `verify_post.py` and `enrich_cards.py` all take the document
itself and neither know nor care which kind it came from.

**The one real asymmetry is slug, and it favours pages.** `page create --slug` sets the slug.
`post create` has no such option, so a post's slug has to go through `--from-json` as described
under [Publishing a file](#publishing-a-file). Verified on 0.16.6.

## Verify — the step that earns this skill

Run it every time, before publishing and again after. It has caught a stale draft, a
front-matter leak, and content that never reached Ghost at all.

```bash
ghst post get <post-id> --json --jq '.posts[0].lexical' > /tmp/lexical.json
uv run ~/.claude/skills/ghost-publish/scripts/verify_post.py /tmp/body.md /tmp/lexical.json
```

**Assert both directions.** This is the rule the script exists to enforce, and it came from a
check that passed sixteen out of sixteen expectations while YAML sat above the first
paragraph. A verification that only asks *"is what I wanted present?"* is blind to everything
that should not be there. So the script compares the whole document both ways and reports
sentences in the source but not in Ghost **and** sentences in Ghost but not in the source.

Three facts about reading a Ghost post back, each of which produces a convincing false alarm:

- **`post get` has no `html` field.** It returns `lexical` and `mobiledoc` only. Asking for
  `.posts[0].html` yields `null`, and a naive checker reads five bytes of nothing as total
  content loss.
- **`post update --markdown-file` converts to native Lexical.** The document becomes heading
  and paragraph nodes, not a markdown card. A reader that looks only for markdown cards
  reports zero words. The source is **not** recoverable from Ghost after this.
- **An inline `?formats=html` query string is refused by the CLI**, not by Ghost: `Endpoint
  path must not include query parameters. Use --query instead.` The error names the fix, and
  `ghst api /posts/<id>/ --query formats=html` **does** return rendered HTML — on 0.16.5 and
  0.16.6 alike. An earlier version of this file called that workaround closed; it was wrong,
  and wrong when written rather than broken by an upgrade.
  Prefer the Lexical tree anyway, for a reason that survives: `html` is Ghost's *rendered
  output*, so diffing against it compares your source with a renderer. `lexical` is the
  document Ghost stores. The script walks it for you.

Check the metadata separately, since none of it lives in the body:

```bash
ghst post get <post-id> --json \
  --jq '.posts[0] | {status, slug, tags: [.tags[]?.name], excerpt_len: (.custom_excerpt|length), feature_image}'
```

## Publishing and scheduling

Email fires on the **publish transition**, not on create — so the newsletter flags belong on
`publish`, `schedule` or `update`, never on `create`:

```bash
ghst post publish <post-id>                                    # no email
ghst post publish <post-id> --newsletter weekly                # sends email now
ghst post schedule <post-id> --at 2026-09-01T06:00:00Z         # publishes then, no email
ghst post unschedule <post-id>
```

Scheduling is core Ghost and works on self-hosted instances; it is not a Ghost(Pro) feature.
Set `status` to `scheduled` with a future `published_at` and, in Ghost's words, *"the post
will be published, email newsletters will be sent (if applicable), and the status of the post
will change to `published`"*.

**A missed schedule publishes late, not never.** If the instance is down at the scheduled
moment, Ghost's default scheduler detects a past-due job and forces it through when it comes
back — the source comments the case explicitly as *"case blog is down"*. What moves is the
email: a post set for 07:00 that publishes at 09:30 mails subscribers at 09:30.

The admin UI's Drafts / Scheduled / Published tabs are `status` filters, so the same views are
available without a browser:

```bash
ghst post list --filter "status:scheduled" --json --jq '.posts[] | {title, published_at}'
```

## Before you publish

**Run the preflight check first.** One command, offline, no network:

```bash
python3 ~/.claude/skills/ghost-publish/scripts/preflight.py
```

It compares the installed `ghst` against the version the traps below were verified on. `ghst`
is pre-1.0 and moves fast — 27 tags and 30 commits in the thirty days to 17 August 2026 — and
the behaviours this skill leans on are undocumented, so no semver promise covers them. **Drift
warns and names what to re-verify; it does not block.** A check that refuses to run the day
`ghst` ships a patch is a check that gets commented out, and then the real drift arrives
unannounced. Use `--strict` in CI, where stopping is the right answer.

Then two habits that belong to the post rather than to Ghost, both learned by getting them
wrong:

**Re-check anything anchored to a live web page.** Citations of papers stay put; a link count
on someone's website, a page that has since 404'd, or a vendor's claim about their own product
all rot between drafting and publishing. Those are the facts to verify last, not first.

**If the post reports numbers about itself, measure after the final edit.** Any change moves
them. Batch every edit, then measure once — `clear-and-human`'s `register_report.py` does this
for register figures. Scope the measurement explicitly ("everything above this heading"),
because a section reporting a document's own numbers is part of that document.

## Provenance

Wraps [`ghst`](https://github.com/TryGhost/ghst) (TryGhost, MIT), installed separately with
`npm i -g @tryghost/ghst`. This skill bundles no credentials and stores none; connection comes
from your own environment.

**`ghst` is beta** (README title, first released February 2026). Its behaviour has been
verified against **0.16.6** — re-check the traps above after an upgrade, since several of them
are undocumented behaviours rather than promises.

The scripts under `scripts/` are standard library only and need **Python 3.12+**. They read
and compare; they never call Ghost and never write to it. `preflight.py` is the one that shells
out at all, and only to `ghst --version`, which runs the local binary and touches no network —
so it can tell you the tool is the one these traps were written against, and cannot tell you
your credentials work.
