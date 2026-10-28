from pathlib import Path

from naga_code import codeintel

PY = b"""
import os

class Shop:
    def add(self, item):
        return item

    class Inner:
        def deep(self):
            pass

async def main():
    pass
"""
TS = b"""
export interface Item { id: number }
export class Cart {
  total(): number { return 0 }
}
export function checkout(c: Cart): void {}
type Alias = string;
"""
GO = b"package main\ntype Server struct{}\nfunc (s *Server) Start() {}\nfunc main() {}\n"
RS = b"struct Point { x: i32 }\nimpl Point { fn new() -> Self { Point { x: 0 } } }\nfn main() {}\n"
CPP = b"namespace app { class Engine { public: void run(); }; }\nint add(int a, int b) { return a + b; }\n"


def names(path, src):
    return [(s.kind, s.qualified) for s in codeintel.symbols(path, src)]


def test_symbols_across_languages():
    assert names("a.py", PY) == [("class", "Shop"), ("function", "Shop.add"), ("class", "Shop.Inner"),
                                 ("function", "Shop.Inner.deep"), ("function", "main")]
    ts = names("a.ts", TS)
    assert ("interface", "Item") in ts and ("class", "Cart") in ts and ("method", "Cart.total") in ts
    assert ("function", "checkout") in ts and ("type", "Alias") in ts
    assert ("struct", "Server") in [(k, n) for k, n in names("a.go", GO)] or ("type", "Server") in names("a.go", GO)
    assert ("method", "Start") in names("a.go", GO) and ("function", "main") in names("a.go", GO)
    rs = names("a.rs", RS)
    assert ("struct", "Point") in rs and ("impl", "Point") in rs and ("function", "Point.new") in rs
    cpp = names("a.cpp", CPP)
    assert ("namespace", "app") in cpp and ("class", "app.Engine") in cpp and ("function", "add") in cpp


def test_syntax_errors_and_unsupported_files():
    assert codeintel.syntax_errors("ok.py", b"def f():\n    return 1\n") == []
    errs = codeintel.syntax_errors("bad.py", b"def f(:\n    return (1\n")
    assert errs and errs[0][0] == 1
    assert codeintel.syntax_errors("bad.ts", b"function f( {") != []
    assert codeintel.symbols("notes.txt", b"anything") == [] and not codeintel.supported("x.lua")


def test_check_after_edit_reports_problems(tmp_path):
    (tmp_path / "good.py").write_text("x = 1\n")
    (tmp_path / "bad.js").write_text("function f( {\n")
    report = codeintel.check_after_edit(Path(tmp_path), ["good.py", "bad.js"])
    assert "bad.js:1" in report and "good.py" not in report


async def test_find_symbol_and_outline_tools(make_app, project):
    (project / "shop.py").write_text(PY.decode())
    app = await make_app()
    from naga_code.tools.registry import ToolCall
    r = await app.registry.execute(ToolCall("find_symbol", {"name": "Shop.add"}), app.ctx)
    assert "shop.py:5: function Shop.add" in r.output
    r = await app.registry.execute(ToolCall("find_symbol", {"name": "deep"}), app.ctx)
    assert "Shop.Inner.deep" in r.output
    r = await app.registry.execute(ToolCall("code_outline", {"path": "shop.py"}), app.ctx)
    assert "class Shop" in r.output and "function main" in r.output
    await app.ensure_repo_map()
    assert "shop.py: Shop, main" in app.repo_map or "shop.py: Shop" in app.repo_map


async def test_edit_with_syntax_error_is_reported_to_the_model(make_app, project):
    script = [{"text": "", "tool_calls": [("read_file", {"path": "calc.py"})]},
              {"text": "", "tool_calls": [("edit_file", {"path": "calc.py", "old_string": "return a - b",
                                                          "new_string": "return (a + b"})]}, "Done."]
    app = await make_app(script)
    await app.run("edit")
    out = [m.content for m in app.agent.history if m.role == "tool" and m.name == "edit_file"][0]
    assert "Automatic check of the edited file(s) found problems" in out and "calc.py:2" in out
