"""Render a representative markdown chunk via rich.markdown.Markdown,
mounted into a Textual Static the same way the real preview pane will,
and save an SVG so we can eyeball it against glow.

Run via: ``PYTHONPATH=. uv run python scripts/snap_markdown.py``
"""

from __future__ import annotations

import asyncio

from rich.markdown import Markdown
from textual.app import App, ComposeResult
from textual.containers import VerticalScroll
from textual.widgets import Static

SAMPLE = """# Strategy Pattern (Workshop)

The **Strategy pattern** lets an object pick an algorithm at runtime by delegating
the work to a family of interchangeable "strategy" objects. Instead of hard-coding
behaviour with `if`/`switch` on a *type*, you swap in a different strategy object.

## Intent

Define a family of algorithms. Encapsulate each one as a separate class. Make
them interchangeable at runtime.

### When to use it

- The object can do its job multiple ways and you want to switch between them.
- Each way has its own state or dependencies.
- The set of ways may grow as you add features.

## Code sketch

```cpp
class SortStrategy {
public:
    virtual ~SortStrategy() = default;
    virtual void sort(std::vector<int>& data) = 0;
};

class QuickSort : public SortStrategy {
public:
    void sort(std::vector<int>& data) override {
        std::sort(data.begin(), data.end());
    }
};
```

## Trade-offs

| Strategy            | Pros                              | Cons                        |
|---------------------|-----------------------------------|-----------------------------|
| Quick sort          | O(n log n) average                | O(n²) worst case            |
| Merge sort          | Stable, O(n log n) guaranteed     | O(n) extra memory           |
| Insertion sort      | Fast for tiny inputs              | O(n²) for large data        |

> Use the **strategy pattern** when behaviour varies independently of the host
> object. Combine with the *Open/Closed Principle*: open for extension (add a
> strategy), closed for modification (don't touch the host).

## Common gotchas

1. Picking the wrong granularity — strategies should be *interchangeable*.
2. Forgetting to provide a sensible default strategy.
3. Strategy objects holding internal state that conflicts with reuse — make
   them stateless or document lifetime carefully.

---

See also: *Template Method*, *Decorator*. ~~Avoid~~ Inheritance for the same
behaviour switch.
"""


class _Demo(App[None]):
    CSS = """
    Screen { background: $surface; }
    #pane {
        border: round $primary 50%;
        padding: 0 1;
        height: 1fr;
    }
    """

    def compose(self) -> ComposeResult:
        with VerticalScroll(id="pane") as v:
            v.border_title = "Preview — strategy_pattern.md (rendered via rich.Markdown)"
            yield Static(Markdown(SAMPLE, code_theme="monokai"))


async def main() -> None:
    app = _Demo()
    async with app.run_test(size=(150, 50)) as pilot:
        await pilot.pause()
        app.save_screenshot(filename="/tmp/fnd_md_demo.svg")


if __name__ == "__main__":
    asyncio.run(main())
