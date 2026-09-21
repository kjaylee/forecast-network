#!/usr/bin/env python3
"""Every route the Python Worker serves must be one the Rust Worker claims.

`sql_parity.py` holds the *statements* to the reference. Nothing held the *surface*: a route the
Rust Worker does not claim is silently forwarded to Python through the service binding, so the two
Workers answer the same request out of two codebases and no check says which. That is the failure
this looks for, and it is a quiet one — a forwarded route works.

Both sides are read from the source rather than transcribed:

  * Python side: `apps/web/src/entry.py` is walked as an AST. A path literal, or a
    `re.fullmatch(...)` result that an `if` tests, is a route; the `method ==` tests enclosing that
    `if` are its methods. Where the entry checks the method *inside* the branch body no static walk
    can see it, and those rows are named in `REVIEWED` with the methods read off the body by hand.

  * Rust side: the three ownership functions in `apps/web-rs/src/routes.rs` are read as data — the
    path literals, the `identifier(path, prefix, suffix)` triples, and the `forecast_id` /
    `profile_card_hash` / `path.starts_with` recognisers — with `owns` and `owns_admin_read` taken
    as GET and `owns_write` split by its `Method::` arms. A claim is a *pattern*, so matching it
    against a concrete served path is a match and not a string comparison.

This is a check, not a vector: run `--check`.
"""

from __future__ import annotations

import argparse
import ast
import pathlib
import re
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
ENTRY = ROOT / "apps/web/src/entry.py"
ROUTES = ROOT / "apps/web-rs/src/routes.rs"

# The reference's two identifier captures, as they appear inside a `re.fullmatch` pattern.
ID_STRICT = r"\(\[A-Za-z0-9]\[A-Za-z0-9_.:-]{0,127}\)"
ID_LOOSE = r"\(\[A-Za-z0-9_.:-]{1,128}\)"

# The rows where the entry checks the method inside the branch body, so no AST walk can attribute
# one. Each is the methods its body actually serves, read by hand; a row added here without reading
# the body is a hole in this check rather than a fix to it.
REVIEWED = {
    "/api/admin/risk/v2/definitions": {"POST"},
    "/api/admin/risk/v2/profiles": {"POST"},
    "/api/admin/risk/v2/bindings": {"POST"},
    "/api/admin/risk/v2/series": {"POST"},
    "/api/admin/risk/v2/operate": {"POST"},
    "/api/admin/risk/v2/bindings/{id}/refresh": {"POST"},
    "/api/admin/risk/v2/bindings/{id}/revoke": {"POST"},
    "/api/admin/risk/v2/feeds/{id}/publish": {"POST"},
    "/api/admin/risk/v2/feeds/{id}/operate": {"POST"},
    "/api/admin/risk/bindings/{id}/refresh": {"POST"},
    "/api/admin/risk/bindings/{id}/revoke": {"POST"},
    "/api/admin/risk/feeds/{id}/publish": {"POST"},
    "/api/admin/sweep": {"POST"},
    "/api/wallet": {"GET"},
    # The one regex that serves two, and the two ways it does not: GET with no action is the market
    # view, POST is the quote and the fill, and each method on the *other* shape falls through to
    # the 404 at the end of the entry. The rows are split accordingly, so the check does not claim
    # a route the entry answers with a 404.
    "/api/forecasts/{id}/market": {"GET"},
    "/api/forecasts/{id}/market(?:/(quote|fill))?": {"POST"},
}

# The entry's own prefix guards, which are not routes.
NOT_A_ROUTE = {"/api/", "/api/admin/", "/api/wallet/", "/api/risk/feeds/", "/api/risk/v2/feeds/"}


def canonical(pattern: str) -> str:
    """The path as a pattern, with the reference's own identifier captures named."""
    out = re.sub(ID_STRICT, "{id}", pattern)
    out = re.sub(ID_LOOSE, "{id}", out)
    return out.replace(r"([0-9a-f]{64})", "{hash}")


def python_surface() -> dict[str, set[str]]:
    tree = ast.parse(ENTRY.read_text(encoding="utf-8"))
    parents = {id(child): node for node in ast.walk(tree) for child in ast.iter_child_nodes(node)}
    owner = {}
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            for sub in ast.walk(node):
                owner[id(sub)] = node.name

    def pattern_arguments(call: ast.Call) -> list[str]:
        """`re.fullmatch(pattern, path)`: the pattern is first, the value second."""
        if not (isinstance(call.func, ast.Attribute) and call.func.attr in {"fullmatch", "match"}):
            return []
        first = call.args[0] if call.args else None
        if isinstance(first, ast.Constant) and isinstance(first.value, str):
            return [first.value]
        return []

    assignments = []  # (function, lineno, name, pattern)
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign) and isinstance(node.value, ast.Call):
            for pattern in pattern_arguments(node.value):
                names = []
                for target in node.targets:
                    if isinstance(target, ast.Name):
                        names.append(target.id)
                    if isinstance(target, ast.Tuple):
                        names.extend(e.id for e in target.elts if isinstance(e, ast.Name))
                for name in names:
                    assignments.append((owner.get(id(node), ""), node.lineno, name, pattern))

    def named(test: ast.AST, function: str, line: int) -> set[str]:
        """The patterns of the names in `test`, resolved by position.

        The entry reuses a few variable names (`match`, `refresh`) for different patterns in
        different branches, so the assignment that counts is the last one at or before the `if`, in
        the same function. Keying by name alone conflates three patterns and invents routes.
        """
        out = set()
        names = {sub.id for sub in ast.walk(test) if isinstance(sub, ast.Name)}
        names |= {
            sub.operand.id
            for sub in ast.walk(test)
            if isinstance(sub, ast.UnaryOp) and isinstance(sub.operand, ast.Name)
        }
        for name in names:
            candidates = [a for a in assignments if a[0] == function and a[2] == name and a[1] <= line]
            if candidates:
                out.add(max(candidates, key=lambda a: a[1])[3])
        return out

    def methods_in(test: ast.AST) -> set[str]:
        found = set()
        for sub in ast.walk(test):
            if not (isinstance(sub, ast.Compare) and isinstance(sub.left, ast.Name) and sub.left.id == "method"):
                continue
            for op, comparator in zip(sub.ops, sub.comparators):
                if isinstance(op, ast.Eq) and isinstance(comparator, ast.Constant):
                    found.add(comparator.value)
                if isinstance(comparator, (ast.Set, ast.Tuple, ast.List)):
                    found |= {e.value for e in comparator.elts if isinstance(e, ast.Constant)}
        return found

    surface: dict[str, set[str]] = {}
    for node in ast.walk(tree):
        if not isinstance(node, ast.If):
            continue
        paths = named(node.test, owner.get(id(node), ""), node.lineno)
        for sub in ast.walk(node.test):
            if isinstance(sub, ast.Call):
                paths |= set(pattern_arguments(sub))
                if isinstance(sub.func, ast.Attribute) and sub.func.attr == "startswith":
                    for argument in sub.args[1:]:
                        if isinstance(argument, ast.Constant) and isinstance(argument.value, str):
                            paths.add(argument.value + "{rest}")
            if isinstance(sub, ast.Constant) and isinstance(sub.value, str) and sub.value.startswith("/api/"):
                paths.add(sub.value)
        if not paths:
            continue
        methods: set[str] = set()
        walked: ast.AST | None = node
        while walked is not None:
            if isinstance(walked, ast.If):
                methods |= methods_in(walked.test)
            walked = parents.get(id(walked))
        for path in paths:
            surface.setdefault(canonical(path), set()).update(methods or {"ANY"})

    for path, methods in REVIEWED.items():
        surface[path] = set(methods)
    return {path: methods for path, methods in surface.items() if path.startswith("/api/") and "[" not in path}


def rust_surface() -> dict[str, set[str]]:
    """The routes the Rust Worker claims: pattern → methods."""
    source = ROUTES.read_text(encoding="utf-8")

    def arm_body(function: str) -> str:
        """One function's body, by brace matching from its `fn`."""
        start = source.index(f"fn {function}(")
        depth = 0
        for index in range(start, len(source)):
            if source[index] == "{":
                depth += 1
            elif source[index] == "}":
                depth -= 1
                if depth == 0:
                    return source[start:index]
        raise AssertionError(function)

    triple = re.compile(r'identifier\(path,\s*"([^"]+)",\s*"([^"]+)"\)')
    literal = re.compile(r'"((?:/api/)[^"]*)"')
    prefix = re.compile(r'path\.starts_with\("((?:/api/)[^"]*)"\)')

    def recognisers(body: str) -> set[str]:
        # A `starts_with` claim is a prefix and covers whatever follows; a `feed_id` claim is one
        # segment. They are different patterns and matching one as the other loses every route
        # under the prefix.
        out = set(literal.findall(body)) | {value + "{any}" for value in prefix.findall(body)}
        for before, after in triple.findall(body):
            out.add(f"{before}{{id}}{after}")
        # The table-driven families: `["/a", "/b"].iter().any(|suffix| identifier(path, PREFIX,
        # &format!("SUFFIX{suffix}")).is_some())`. One rule covers all three, and a family whose
        # suffix is built with `format!` is invisible to a literal-only reader — which is how the
        # market and attestation routes first read as unclaimed.
        family = re.compile(
            r'\[([^\]]+)\]\s*\.iter\(\)\s*\.any\(\s*\|(\w+)\|\s*'
            r'identifier\(path,\s*"([^"]+)",\s*&format!\("([^"]+)"\)\)'
        )
        for literals, variable, before, template in family.findall(body):
            # The prefix already ends in `/` and so does the template: joining them naively claims
            # `/api/forecasts//market/quote`, which matches nothing.
            for one in re.findall(r'"([^"]+)"', literals):
                # `identifier` puts the identifier between the prefix and the suffix, so the claim
                # has one even when both halves are literals — and the prefix keeps its own
                # trailing slash rather than being joined to the suffix directly.
                out.add(f"{before}{{id}}" + template.replace("{" + variable + "}", one))
        if "forecast_id(path)" in body:
            out.add("/api/forecasts/{id}")
        if "profile_card_hash(path)" in body:
            out.add("/api/profile-cards/{hash}")
        for name in ("/api/risk/feeds/", "/api/risk/v2/feeds/"):
            if name in body:
                out.add(f"{name}{{rest}}")
        return out

    claimed: dict[str, set[str]] = {}
    for path in recognisers(arm_body("owns")):
        claimed.setdefault(path, set()).add("GET")
    for path in recognisers(arm_body("owns_admin_read")):
        claimed.setdefault(path, set()).add("GET")
    writes = arm_body("owns_write")
    patch = writes.index("Method::Patch")
    post = writes.index("Method::Post")
    for path in recognisers(writes[patch:post]):
        claimed.setdefault(path, set()).add("PATCH")
    for path in recognisers(writes[post:]):
        claimed.setdefault(path, set()).add("POST")
    return claimed


def instantiate(path: str) -> list[str]:
    """Concrete paths for a served pattern, with the reference's inline alternations expanded.

    The two shapes the entry writes are handled as *text*: `(?:/(a|b))?` and `(a|b|c)`. A regex
    here would be matching a regex with a regex, and the one time this was written that way it
    silently matched nothing — a check that reports every route as unclaimed is louder than one
    that reports none, but it is wrong for the same reason.
    """
    base = path.replace("{id}", "f_1").replace("{hash}", "a" * 64).replace("{rest}", "x").replace("{any}", "x")
    optional = "(?:/"
    if optional in base:
        # `(?:/(a|b))?` — the branches are the routes; the bare path is not one of them.
        head, rest = base.split(optional, 1)
        branches, tail = rest.split(")?", 1)
        assert tail == "" and branches.startswith("(") and branches.endswith(")"), base
        return [f"{head}/{branch}" for branch in branches[1:-1].split("|")]
    if "(" in base:
        head, rest = base.split("(", 1)
        branches, tail = rest.split(")", 1)
        return [head + branch + tail for branch in branches.split("|")]
    return [base]


def matcher(pattern: str) -> re.Pattern:
    """A claimed pattern as something that can be matched against a concrete path."""
    body = re.escape(pattern)
    body = body.replace(re.escape("{id}"), r"[A-Za-z0-9_.:-]{1,128}")
    body = body.replace(re.escape("{hash}"), r"[0-9a-f]{64}")
    body = body.replace(re.escape("{rest}"), r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}")
    body = body.replace(re.escape("{any}"), r".+")
    return re.compile(body)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true", help="fail when a route is not claimed")
    arguments = parser.parse_args()
    if not arguments.check:
        parser.error("this script only reports drift; pass --check")

    served = python_surface()
    claims = [(matcher(pattern), methods) for pattern, methods in rust_surface().items()]

    missing = []
    checked = 0
    for path, methods in sorted(served.items()):
        if path in NOT_A_ROUTE:
            continue
        for method in sorted(methods):
            for one in instantiate(path):
                checked += 1
                if any(method in held and pattern.fullmatch(one) for pattern, held in claims):
                    continue
                missing.append((method, path, one))

    for method, path, one in missing:
        print(f"unclaimed: {method} {path}  ({one})", file=sys.stderr)
    if missing:
        print(f"route parity: {len(missing)} of {checked} served routes are not claimed", file=sys.stderr)
        return 1
    print(f"route parity verified: {checked} served routes, all claimed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
