# Concurrency and rare races

*Read before adding a lock, a guard against interleaving, or a defensive re-read - and before acting on any review finding that claims a race.*

- Write defensively against failures that actually happen: Discord errors,
  missing permissions, rows that disappear, restarts mid-workflow, and stale
  snapshots held while a confirmation sits open. Re-read state before mutating
  it, and clean up after a write that fails part-way.
- Do not chase sub-second interleavings - a race that needs two commanders, or
  a commander and the maintenance pass, colliding inside the same few hundred
  milliseconds. Re-reading before the mutation is the accepted mitigation for
  these; a further guard is not worth its cost.
- Reject review findings of that shape, including automated ones, rather than
  acting on them. Say plainly that the interleaving is too rare to be worth
  the change, and move on.
- Weigh any such guard against the asynchronous design, which comes first:
  holding locks across Discord I/O for whole workflows, serialising the event
  loop, or taking broad mutation locks over central paths costs more than the
  races it closes.
- A race actually observed in production is a different matter. Fix that one
  deliberately, with the evidence in hand.
