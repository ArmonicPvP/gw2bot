# The core/ boundary

*Read before adding to `src/gw2bot/core/`, before moving anything into it, and when `tests/test_layout.py` fails.*

`core/` holds what every feature shares and nothing that knows about a
feature: the SQLite engine and its migrations, the redacting log formatter,
the Discord role and failure helpers, the anchored-series maths, and the
vocabulary a dashboard range is written in.

The dependency only ever points one way. Any package may import `core/`;
nothing in `core/` may import anything else under `gw2bot` - not a feature
package, not `gw2/`, not `bot.py`, not `config.py`. A single-dot relative
import stays inside `core/` and is fine; `from ..config import Config` is the
same escape as the absolute spelling and is rejected the same way.

`tests/test_layout.py` parses every module under `core/` and asserts this, so
the boundary is a failing test rather than something review has to catch. It
judges an import by where it lands: relative spellings are resolved against
the importing file's own package, and imports are read anywhere in the file,
so none of a `..` prefix, a function body or an `if TYPE_CHECKING:` block
gets around it.

The rule is the whole point of the directory. Without it `core/` becomes the
`utils/` folder that collects whatever had nowhere else to go, and the
layering it is supposed to express stops meaning anything. "Several features
use it" is not the test: something belongs in `core/` only if it is useful
without knowing what a raffle, an event or a Trial member is.

When something in `core/` looks like it needs a feature:

- Used by one feature - it belongs in that feature's package, not here.
- Used by several - take what it needs as an argument or a protocol instead
  of importing the feature to go and get it.
- Genuinely a new shared vocabulary - that is a new module in `core/`, not an
  import out of an existing one.
- Needed only for a type annotation - annotate against a protocol defined in
  `core/`, or leave the annotation as a string.

`gw2/` is *not* under this rule and no test asserts anything about it.
`gw2/guild_log.py` imports `raffle` to parse gold deposits and render their
embed, so the layer is deliberately not pure, and extending the assertion to
`gw2/` would fail until that import is dealt with.
