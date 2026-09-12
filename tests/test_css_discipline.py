"""设计令牌纪律守卫：品牌色令牌在 :root 定义、无循环引用、组件不得写死品牌色。"""

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CSS_PATH = ROOT / "frontend" / "src" / "styles" / "main.css"
BRAND_TOKENS = ("--brand", "--brand-2", "--brand-light", "--brand-rgb")
BRAND_LITERALS = ("#d97852", "#c4603a", "#f3b599", "rgba(217,120,82")

_VAR_RE = re.compile(r"var\(\s*(--[\w-]+)")


def _root_block(css: str) -> str:
    """取 :root { ... } 块内容，缺失直接失败。"""
    m = re.search(r":root\s*\{([^}]*)\}", css, re.S)
    assert m, ":root 设计令牌块缺失"
    return m.group(1)


def _declarations(css: str) -> dict[str, str]:
    """返回 {--x: value}：解析文件中所有 `--x: value;` 声明。"""
    decls: dict[str, str] = {}
    for m in re.finditer(r"(--[\w-]+)\s*:\s*([^;]+);", css):
        decls[m.group(1)] = m.group(2).strip()
    return decls


def test_brand_tokens_declared_in_root_and_concrete():
    css = CSS_PATH.read_text(encoding="utf-8")
    root = _root_block(css)
    for token in BRAND_TOKENS:
        m = re.search(rf"^\s*{token}\s*:\s*(.+?);\s*$", root, re.M)
        assert m, f"品牌令牌 {token} 必须定义在 :root 中"
        assert not m.group(1).strip().startswith("var("), f"{token} 必须为具体值"


def test_custom_property_graph_has_no_cycles_or_undefined_vars():
    css = CSS_PATH.read_text(encoding="utf-8")
    decls = _declarations(css)
    assert decls, "main.css 未找到自定义属性声明"
    graph = {name: set(_VAR_RE.findall(value)) for name, value in decls.items()}
    for name, refs in graph.items():
        for ref in refs:
            assert ref in decls, f"{name} 引用了未定义的自定义属性 {ref}"
    WHITE, GRAY, BLACK = 0, 1, 2
    color = {k: WHITE for k in graph}

    def find_cycle(node: str, path: list[str]) -> list[str] | None:
        color[node] = GRAY
        path.append(node)
        for nxt in graph[node]:
            if color[nxt] == GRAY:
                return path[path.index(nxt) :] + [nxt]
            if color[nxt] == WHITE:
                cycle = find_cycle(nxt, path)
                if cycle:
                    return cycle
        path.pop()
        color[node] = BLACK
        return None

    for node in graph:
        if color[node] == WHITE:
            cycle = find_cycle(node, [])
            if cycle:
                raise AssertionError("自定义属性存在循环引用: " + " → ".join(cycle))


def test_components_do_not_hardcode_brand_colors():
    pattern = re.compile("|".join(re.escape(lit) for lit in BRAND_LITERALS), re.IGNORECASE)
    for path in sorted((ROOT / "frontend" / "src").rglob("*.vue")):
        text = re.sub(r"\s+", "", path.read_text(encoding="utf-8")).lower()
        assert not pattern.search(text), f"{path} 中写死了品牌色，请改用 var(--brand*)"
