from rich.text import Text

import fnd.tui.mermaid_render as m
from fnd.tui.mermaid_render import MermaidRenderer

FLOW = "flowchart TD\n  A[Start] --> B[End]"


def test_valid_flowchart_returns_text():
    out = MermaidRenderer().render(FLOW)
    assert isinstance(out, Text)
    assert out.plain.strip()


def test_garbage_returns_none():
    assert MermaidRenderer().render("this is not mermaid") is None


def test_empty_returns_none():
    assert MermaidRenderer().render("   ") is None


def test_oversized_returns_none_without_rendering(monkeypatch):
    called = {"n": 0}

    def boom(*a, **k):
        called["n"] += 1
        raise AssertionError("should not render oversized source")

    monkeypatch.setattr(m.termaid, "render_rich", boom)
    huge = "flowchart TD\n" + "\n".join(f"  N{i} --> N{i + 1}" for i in range(300))
    # bypass the cache so the guard (not a cached None) is what returns None
    m._render_cached.cache_clear()
    assert MermaidRenderer().render(huge) is None
    assert called["n"] == 0


def test_cache_hit_does_not_reinvoke(monkeypatch):
    m._render_cached.cache_clear()
    calls = {"n": 0}
    real = m.termaid.render_rich

    def counting(*a, **k):
        calls["n"] += 1
        return real(*a, **k)

    monkeypatch.setattr(m.termaid, "render_rich", counting)
    r = MermaidRenderer()
    r.render(FLOW)
    r.render(FLOW)
    assert calls["n"] == 1


def test_ascii_mode_differs():
    r = MermaidRenderer()
    uni = r.render(FLOW, use_ascii=False)
    asc = r.render(FLOW, use_ascii=True)
    assert uni is not None
    assert asc is not None
    assert "┌" in uni.plain
    assert "┌" not in asc.plain
