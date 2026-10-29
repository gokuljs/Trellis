# Review the Stage 7 branch stack

The user opens the PRs and owns their titles, descriptions, and merge decisions.
Implementation delivery consists of verified atomic commits on pushed branches.
The branch names have no `codex/` prefix.

## Branch order

Open each PR against its listed base to isolate that layer's diff. Review and
merge from the first row downward. Keep descendants aligned after a parent merge.

| Order | Branch                            | Base                         | Boundary                                        | Status      |
| ----- | --------------------------------- | ---------------------------- | ----------------------------------------------- | ----------- |
| 1     | `search-and-crawler`              | `main`                       | Architecture and acceptance contract            | Documented  |
| 2     | `search-and-crawler-runtime`      | `search-and-crawler`         | Trusted tool context and capability separation  | Verified    |
| 3     | `search-and-crawler-sources`      | `search-and-crawler-runtime` | Evidence, RLS, source APIs, budgets, recovery   | Verified    |
| 4     | `search-and-crawler-fetch`        | `search-and-crawler-sources` | Isolated worker and controlled public retrieval | Planned     |
| 5     | `search-and-crawler-reading`      | `search-and-crawler-fetch`   | Brave, HTML, PDF, and source tools              | Planned     |
| 6     | `search-and-crawler-browser`      | `search-and-crawler-reading` | Sandboxed browser rendering                     | Planned     |
| 7     | `search-and-crawler-crawl`        | `search-and-crawler-browser` | Bounded crawl and persistent frontier           | Planned     |
| 8     | `search-and-crawler-chat`         | `search-and-crawler-crawl`   | Progress, citations, source viewer, clearing    | Planned     |
| 9     | `search-and-crawler-verification` | `search-and-crawler-chat`    | Complete acceptance and operator instructions   | Planned     |

## Implementation checkpoints

1. `how` over the affected subsystem. Completed during design and checked against
   the current runtime before implementation.
2. `architect` for parallel design exploration. Completed for runtime, storage,
   and retrieval boundaries. The approved architecture is in
   [the design report](stage-7-web.md).
3. Blocking first steps. Verify the current branch, required guidance, execution
   contracts, dependencies, and baseline before enabling a new layer.
4. Independent workstreams. The implementation owner handles coupled code.
   Root handles documentation, review, Git, and verification. Owner delegates
   disjoint code only after shared contracts are settled.
5. Shared mutable state. Git operations are root-owned. Source files have one
   writer at a time. Database invariants use atomic mutation operations.
6. Smallest safe decomposition. Each row ends in a separately checked branch.
   Runtime and SQL recovery changes stay coherent within the evidence layer.
7. Verify each unit on its actual boundary, then commit and push it.
8. Skip Opening a PR. The user explicitly owns PR creation and metadata.

## Verification evidence

Each implementation layer adds its tests with the behavior. The last branch
consolidates full acceptance evidence rather than postponing security testing.
Required backend checks use pinned uv 0.12.5. Database checks use a disposable
local database. Runtime changes must preserve current tool and approval tests.

The final handoff lists pushed branch tips, comparison links, commit summaries,
commands run, outcomes, setup requirements, and unverified limitations.
No production deployment, live database migration, or provider billing activation
is part of pushing the branches.
