# Composite tasks / 复合操作

One sentence, several different operations.

Trigger words: 并、然后、同时、之后、接着、再、以及、顺便、配置、布置、装修、带有、包含、有窗、有门、加上、还要 / and then, also, after that, with, including, furnish, layout, need, contain.

## When to split

Split when the request needs several different API operations: "create a room and furnish it" -> walls, room, furniture. Do not split N identical elements ("three walls" is one operation with N = 3).

## Rules

1. Each step is its own operation with its own parameters; the pattern for that operation applies in full. "We asked earlier" is not a source for a later step.
2. State dependencies: "door in the wall from step 1" - the host of step 2 is the result of step 1, so step 2 is drafted after step 1 has run and reported its id.
3. Shared parameters (level, type) are asked once and written on every step's card lines with the same source.
4. One spec card lists all steps in order; confirm once for the whole plan, then execute step by step, reporting each result before the next.

## Common mistakes

- Skipping questions for later steps because the first step "covered it".
- Executing step 2 before step 1's result is known.
