# Versioning and releases

*Read before creating a branch, opening a pull request, or changing anything an operator or member relies on: a command or its options, a setting, an environment variable, a web address, the database, the Docker image, or a persistent button. Also before touching the release workflow or `scripts/next_release.py`.*

The bot follows [Semantic Versioning 2.0.0](https://semver.org/). README.md's
"Versioning And Releases" section is the operator's account of it, including
the public API the number describes; keep the two in step.

## The branch decides the release

A pull request's head branch names the kind of change, and the `Release`
workflow numbers the next version from it. The `Branch name` CI check refuses
anything else.

| Branch | When | Release |
| --- | --- | --- |
| `feat/<name>` | A member or operator can do something they could not before: a command, option, setting, page, notification. | MINOR |
| `fix/<name>` | Something that already existed now does what it should. | PATCH |
| `chore/<name>` | Nothing a member or operator would notice: tests, docs, CI, refactoring, agent files. | none |

- `<name>` is lowercase words joined by single hyphens: `feat/profit-page`,
  `fix/raffle-rounding`. One level only - no further `/`.
- A pull request that mixes kinds takes the largest one. A fix that ships with
  a new option is `feat/`.
- A change to what ships that is neither a feature nor a fix - a runtime
  dependency bump, a Dockerfile change - is `chore/` and waits for the next
  release, unless it is worth releasing on its own; then say so in the pull
  request so the maintainer can run the workflow by hand.

## Breaking changes

A change is breaking when anything in the public API is removed or renamed, or
when an existing deployment needs an operator to act before or after the
upgrade. For example:

- renaming or removing an environment variable, `/settings` subcommand, slash
  command or option
- a database change the bot cannot apply to an existing file by itself on
  startup
- a new required setting with no default that keeps the old behaviour
- moving the data volume, changing the image's UID or GID, or changing a web
  address, including `/oauth/callback`
- changing the `custom_id` of a persistent button, which breaks every message
  already posted with it

Prefer the compatible path: add the new thing alongside the old, mark the old
one deprecated (that is a MINOR release), and remove it in a later major
version. `settings/migration.py` importing the variables `/settings` replaced
is the model.

When a change is breaking anyway, start the pull request description with
`BREAKING CHANGE:` and what an operator must do, and ask the user to add the
`breaking` label. The label, not the branch, is what makes it a MAJOR release.

## Where the number lives

Only in the `vX.Y.Z` tags the `Release` workflow creates.

- Never record the current version by hand - not in the source, the README or
  a commit message - and never create, move or delete a `v` tag.
- `gw2bot.version.__version__` is what the running bot reports. The image build
  writes `src/gw2bot/VERSION` from the `GW2BOT_VERSION` build argument; any
  other copy reports `0.0.0-dev`. The stamp is ignored by Git and by the Docker
  build context, and must stay that way.
- `scripts/next_release.py` holds the branch table, the label, the first
  version and the bump rules for both the CI check and the release. Its tests
  are in `tests/test_versioning.py`, and run `plan` against real Git
  repositories; add a case there for any rule you change.
