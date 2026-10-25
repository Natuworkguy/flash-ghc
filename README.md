# GitHub Copilot for Flash

A [Flash](https://github.com/Natuworkguy/Flash) extension that adds the
models of your GitHub Copilot subscription as a model provider. Once it
is installed, they are picked like any other model:

```text
/model @ghc/gpt-4.1
```

They also show in the web UI's model menu, can be a spark's model, and
run sub-agents. Tools, images, thinking, and streaming in the web UI all
work.

## Install

```text
/extension install github@Natuworkguy/flash-ghc
```

Or from a folder, to try it out: `/extension install path@/path/to/flash-ghc`.

## Sign in

If you have signed in with the [GitHub CLI](https://cli.github.com)
(`gh auth login`), there is nothing to set: the extension asks `gh` for
its token. Otherwise give it a token that can use Copilot. The easiest
place is its own settings: on Flash's Extensions page, **Settings** on
ghc, then **API key**. It shows as bullets and is never shown again, only
that it is saved. In the terminal:

```text
/extension set ghc api_key gho_...
```

That token goes ahead of one in the environment, so it works whatever
`COPILOT_GITHUB_TOKEN` is. Leave it empty to use the environment
(`/set COPILOT_GITHUB_TOKEN gho_...`), or nothing at all if you signed in
with `gh`.

The token is looked for in this order: the API key setting,
`COPILOT_GITHUB_TOKEN`, `GH_TOKEN`, `GITHUB_TOKEN`, then `gh auth token`.
The first one GitHub accepts for Copilot is used. Classic personal access
tokens (`ghp_...`) cannot use Copilot and are skipped.

## Models

`/model` lists what Copilot offers your account, so it changes with your
plan. Names are Copilot's own ids, such as `@ghc/gpt-4.1`. Copilot sometimes names a model
your plan cannot use; asking for it says so.

Copilot serves each model on one of two endpoints, `/chat/completions` or
`/responses`. The extension speaks both and picks the one the model's
listing names, so you never have to know which.

| Setting | Meaning |
| --- | --- |
| `COPILOT_EFFORT` | `low`, `medium`, `high`, `xhigh` or `max`, for models that reason. A level the model lacks becomes its nearest one. A reply Flash asks to think harder always goes as hard as the model does. Unset, the model's own default. |
| `COPILOT_ALL_MODELS` | `1` lists every chat model Copilot names, not only those it puts forward. |

Set them with `/set NAME VALUE`.

## How it works

Flash runs `provider.py` once per request, with JSON on stdin (see
Flash's `docs/EXTENSIONS.md`, Providers). It uses only the standard
library, so nothing is installed.

- **The API** your account is served by (individual, business or
  enterprise) is found from GitHub and kept for a day; the model list for
  ten minutes. Both live in `.cache/` in the extension's folder.
- **Replies are replayed as written.** Flash keeps only the text and tool
  calls of a reply, but the Responses API wants the model's encrypted
  reasoning back on the next step. Each reply is kept in `.replies/`, the
  newest 2000, and sent back as the model wrote it.
- **Thinking** is shown as the model's reasoning summary.
- **A cut-off tool call is never run**: if a reply runs out of room in
  the middle of a tool call, Flash is told so instead.

Your conversations go to GitHub Copilot, so `/stats` never counts these
models as running on your own machine.

MIT licensed. See [LICENSE](LICENSE).
