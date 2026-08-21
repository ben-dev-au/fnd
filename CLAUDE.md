# Conventions

## Spelling

Use Australian/British spelling throughout — identifiers, comments, docstrings,
documentation, and commit messages (`sanitise`, `normalise`, `colour`,
`behaviour`, `serialise`, `centre`, `cancelled`).

The only exception is third-party API surface that dictates American spelling
(Rich/Textual `color`, CSS properties, JSON `serialize`); match the library
there, but keep our own names British.

## Comments and docstrings

A comment earns its place only by stating something the code cannot: a
constraint, an invariant, a measured number, or the bug it guards against.
Default to none — well-named identifiers do not need narrating.

**Budget: three lines.** Longer needs a specific reason — a table of
measurements whose numbers are the content, or a module docstring carrying
architecture. Comments inside a function body essentially never qualify.

State the fact, not the story. No history (`this used to be X`), no arguing for
the change (`removed rather than deprioritised`), no recap of the symptom that
prompted it — those belong in the commit message. Architecture rationale lives
in the module docstring once; functions point at it rather than restating it.

The same applies to docstrings and to tests: one line stating the contract.
