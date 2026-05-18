"""Repo code statistics. Run before/after a refactor and diff the outputs.

    python3 code_stats.py
    python3 code_stats.py --top 20
    python3 code_stats.py --paths main.py utils.py

Stdlib only. Docstrings count as SLOC: they carry the *why* and shouldn't
show up as "deleted code" when a refactor moves them.

Metrics, and what they tell you when a refactor is going well:

  Function-length p90 / max DOWN
      The worst offenders are shrinking. A drop in the average alone
      can hide a single 400-line function.

  Function count UP, SLOC roughly flat
      Work got broken into named pieces. More names == more docs.

  Cyclomatic complexity p90 / max DOWN
      Branch-heavy code is being split or simplified.

  Max nesting depth DOWN
      Early-return / guard-clause refactors are landing.

  Fattest file SLOC DOWN, file count UP
      main.py et al. are losing weight to new modules.

  Avg function name length UP (modestly)
      New helpers tend to have descriptive names; one-letter helpers
      drag the average down.
"""
import argparse
import ast
import re
import sys
from pathlib import Path
from statistics import mean


SKIP_DIRS = {
    "__pycache__", ".git", ".claude", "debug", "map_data",
    "docs", "docker", "proxy",
}
TODO_RE = re.compile(r"\b(TODO|FIXME|HACK|XXX)\b")

NEST_NODES = (ast.If, ast.For, ast.AsyncFor, ast.While,
              ast.With, ast.AsyncWith, ast.Try)
FN_NODES = (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)


def discover_py_files(root, only_paths=None):
    if only_paths:
        return [Path(p).resolve() for p in only_paths if Path(p).exists()]
    out = []
    for p in root.rglob("*.py"):
        if any(part in SKIP_DIRS for part in p.relative_to(root).parts):
            continue
        out.append(p)
    return sorted(out)


def count_sloc(text):
    n = 0
    for line in text.splitlines():
        s = line.strip()
        if not s or s.startswith("#"):
            continue
        n += 1
    return n


def count_todos(text, rel):
    """Only flag markers that appear inside a `#` comment -- otherwise
    string literals mentioning these words (regex sources, log messages,
    section titles) trigger false positives."""
    matches = []
    for i, line in enumerate(text.splitlines(), 1):
        hash_at = line.find("#")
        if hash_at == -1:
            continue
        m = TODO_RE.search(line[hash_at:])
        if m:
            matches.append((str(rel), i, m.group(1), line.strip()))
    return matches


def compute_complexity(func_node):
    """McCabe complexity: 1 + branch points. Nested defs/lambdas are
    skipped -- they get their own score."""
    complexity = 1

    def walk(node):
        nonlocal complexity
        for child in ast.iter_child_nodes(node):
            if isinstance(child, FN_NODES):
                continue
            if isinstance(child, (ast.If, ast.For, ast.AsyncFor, ast.While,
                                  ast.IfExp, ast.With, ast.AsyncWith,
                                  ast.Assert)):
                complexity += 1
            elif isinstance(child, ast.Try):
                complexity += len(child.handlers)
            elif isinstance(child, ast.BoolOp):
                complexity += len(child.values) - 1
            elif isinstance(child, ast.comprehension):
                complexity += len(child.ifs)
            elif hasattr(ast, "Match") and isinstance(child, ast.Match):
                complexity += len(child.cases)
            walk(child)

    walk(func_node)
    return complexity


def compute_max_depth(func_node):
    """Deepest control-flow nesting inside the function body. The `def`
    is depth 0; an `if` directly under it is depth 1."""
    def walk(node, depth):
        if isinstance(node, FN_NODES) and node is not func_node:
            return depth
        max_d = depth
        for child in ast.iter_child_nodes(node):
            d = depth + 1 if isinstance(child, NEST_NODES) else depth
            max_d = max(max_d, walk(child, d))
        return max_d
    return walk(func_node, 0)


class FunctionVisitor(ast.NodeVisitor):
    def __init__(self, rel):
        self.rel = rel
        self.functions = []

    def visit_FunctionDef(self, node):
        self._record(node)
        self.generic_visit(node)

    def visit_AsyncFunctionDef(self, node):
        self._record(node)
        self.generic_visit(node)

    def _record(self, node):
        start = node.lineno
        end = getattr(node, "end_lineno", start)
        params = len(node.args.args) + len(node.args.kwonlyargs)
        if node.args.vararg:
            params += 1
        if node.args.kwarg:
            params += 1
        self.functions.append({
            "file": self.rel,
            "name": node.name,
            "line": start,
            "length": end - start + 1,
            "params": params,
            "complexity": compute_complexity(node),
            "depth": compute_max_depth(node),
            "has_doc": ast.get_docstring(node) is not None,
        })


def analyze_file(path, root):
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as e:
        print(f"[code_stats] skip {path}: {e}", file=sys.stderr)
        return None
    try:
        tree = ast.parse(text, filename=str(path))
    except SyntaxError as e:
        print(f"[code_stats] syntax error in {path}: {e}", file=sys.stderr)
        return None
    rel = path.relative_to(root)
    imports = []
    for node in ast.iter_child_nodes(tree):
        if isinstance(node, ast.Import):
            imports.extend(a.name for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imports.append(node.module)
    visitor = FunctionVisitor(str(rel))
    visitor.visit(tree)
    return {
        "path": rel,
        "sloc": count_sloc(text),
        "functions": visitor.functions,
        "imports": imports,
        "todos": count_todos(text, rel),
    }


def percentile(values, p):
    if not values:
        return 0
    s = sorted(values)
    if p <= 0:
        return s[0]
    if p >= 100:
        return s[-1]
    k = (len(s) - 1) * (p / 100)
    f = int(k)
    c = min(f + 1, len(s) - 1)
    if f == c:
        return s[f]
    return s[f] + (s[c] - s[f]) * (k - f)


def section(title):
    print()
    print("=" * 78)
    print(title)
    print("=" * 78)


def main():
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--top", type=int, default=10,
                        help="size of Top-N lists (default 10)")
    parser.add_argument("--paths", nargs="*",
                        help="restrict to these files (relative ok)")
    args = parser.parse_args()

    root = Path(__file__).resolve().parent
    files = discover_py_files(root, args.paths)
    if not files:
        print("[code_stats] no python files found")
        return

    reports = [r for r in (analyze_file(f, root) for f in files) if r]
    funcs = [f for r in reports for f in r["functions"]]
    lengths = [f["length"] for f in funcs]
    cx = [f["complexity"] for f in funcs]
    depths = [f["depth"] for f in funcs]
    params = [f["params"] for f in funcs]
    name_lens = [len(f["name"]) for f in funcs]
    undoc = [f for f in funcs if not f["has_doc"]]
    total_sloc = sum(r["sloc"] for r in reports)
    todos = [t for r in reports for t in r["todos"]]

    stems = {r["path"].stem for r in reports}
    dependents = {}
    fanout = {}
    for r in reports:
        local = 0
        for imp in r["imports"]:
            top = imp.split(".")[0]
            if top in stems and top != r["path"].stem:
                local += 1
                dependents.setdefault(top, set()).add(str(r["path"]))
        fanout[str(r["path"])] = local

    section("Overview")
    print(f"Files            : {len(reports)}")
    print(f"Total SLOC       : {total_sloc}")
    print(f"Functions        : {len(funcs)}")
    if funcs:
        print(f"SLOC / function  : {total_sloc / len(funcs):.1f}")

    section("Function length (lines, def..end inclusive)")
    if lengths:
        print(f"mean : {mean(lengths):.1f}")
        print(f"p50  : {percentile(lengths, 50):.0f}")
        print(f"p90  : {percentile(lengths, 90):.0f}")
        print(f"max  : {max(lengths)}")

    section("Cyclomatic complexity")
    if cx:
        print(f"mean : {mean(cx):.1f}")
        print(f"p50  : {percentile(cx, 50):.0f}")
        print(f"p90  : {percentile(cx, 90):.0f}")
        print(f"max  : {max(cx)}")

    section("Max nesting depth")
    if depths:
        print(f"mean : {mean(depths):.1f}")
        print(f"p90  : {percentile(depths, 90):.0f}")
        print(f"max  : {max(depths)}")

    section("Parameters per function")
    if params:
        print(f"mean : {mean(params):.1f}")
        print(f"p90  : {percentile(params, 90):.0f}")
        print(f"max  : {max(params)}")

    section("Self-documentation")
    if name_lens:
        print(f"avg function name length : {mean(name_lens):.1f} chars")
        print(f"functions without docstring : {len(undoc)} / {len(funcs)} "
              f"({100 * len(undoc) / len(funcs):.0f}%)")

    section(f"Top {args.top} longest functions")
    print(f"{'len':>5}  {'cx':>3}  {'depth':>5}  location")
    for f in sorted(funcs, key=lambda f: -f["length"])[:args.top]:
        print(f"{f['length']:>5}  {f['complexity']:>3}  {f['depth']:>5}  "
              f"{f['file']}:{f['line']}  {f['name']}")

    section(f"Top {args.top} most complex functions")
    print(f"{'cx':>3}  {'len':>5}  {'depth':>5}  location")
    for f in sorted(funcs, key=lambda f: -f["complexity"])[:args.top]:
        print(f"{f['complexity']:>3}  {f['length']:>5}  {f['depth']:>5}  "
              f"{f['file']}:{f['line']}  {f['name']}")

    section(f"Top {args.top} fattest files")
    print(f"{'sloc':>5}  {'fns':>4}  {'fanout':>6}  file")
    for r in sorted(reports, key=lambda r: -r["sloc"])[:args.top]:
        print(f"{r['sloc']:>5}  {len(r['functions']):>4}  "
              f"{fanout.get(str(r['path']), 0):>6}  {r['path']}")

    section(f"Top {args.top} most-imported local modules")
    deps = sorted(dependents.items(), key=lambda kv: -len(kv[1]))[:args.top]
    if deps:
        print(f"{'deps':>4}  module")
        for name, s in deps:
            print(f"{len(s):>4}  {name}")
    else:
        print("(no local imports detected)")

    section(f"TODO / FIXME / HACK / XXX markers ({len(todos)})")
    if todos:
        for path, line, tag, text in todos:
            print(f"  {path}:{line}  [{tag}]  {text[:90]}")
    else:
        print("(none)")
    print()


if __name__ == "__main__":
    main()
