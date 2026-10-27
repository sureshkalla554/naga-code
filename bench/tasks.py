"""Benchmark tasks for bench/regression.py: starting files, the prompt, a hidden checker and a reference solution.

Each checker gets the task directory after the agent finished and returns (score 0-100, notes).
Checkers run in a fresh subprocess with a timeout, so a broken solution can't hang or crash the harness.
"""
import json
import subprocess
import sys
import textwrap
from pathlib import Path

D = textwrap.dedent


def _run(argv, cwd, timeout, input=None) -> subprocess.CompletedProcess:
    try:
        return subprocess.run(argv, cwd=cwd, capture_output=True, text=True, timeout=timeout, input=input)
    except subprocess.TimeoutExpired:
        return subprocess.CompletedProcess(argv, -9, "", f"timed out after {timeout} s")


def py(code: str, cwd: Path, timeout: float = 60, input: str | None = None) -> subprocess.CompletedProcess:
    return _run([sys.executable, "-c", code], cwd, timeout, input)


def pytest(cwd: Path, *args: str, timeout: float = 120) -> subprocess.CompletedProcess:
    return _run([sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", *args], cwd, timeout)


def hidden_tests(cwd: Path, name: str, code: str) -> tuple[int, int]:
    """Run hidden pytest code against the solution; returns (passed, total). The file is removed afterwards."""
    f = cwd / f"_hidden_{name}.py"
    f.write_text(code)
    try:
        r = pytest(cwd, f.name, "-rN")
    finally:
        f.unlink(missing_ok=True)
    out = r.stdout.splitlines()[-1] if r.stdout.strip() else ""
    passed = int(next((w.split()[0] for w in out.split(",") if "passed" in w), 0) or 0)
    failed = sum(int(w.split()[0]) for w in out.split(",") if "failed" in w or "error" in w)
    return passed, passed + failed


TASKS: dict[str, dict] = {}


def task(tid, title, prompt, files, check, reference):
    TASKS[tid] = {"title": title, "prompt": prompt, "files": files, "check": check, "reference": reference}


# ---------------------------------------------------------------------------------------------- 1
task("t01", "Fix pagination bug",
     "The tests in test_pager.py fail. Find and fix the bug in pager.py so all tests pass. Do not modify the tests.",
     {"pager.py": D('''
        def paginate(items, page, per_page):
            """Return the items on `page` (1-based) when showing `per_page` items per page.

            page < 1 or per_page < 1 raises ValueError. Pages past the end return [].
            """
            if page < 1 or per_page < 1:
                raise ValueError("page and per_page must be >= 1")
            start = page * per_page
            return items[start:start + per_page]


        def page_count(n_items, per_page):
            """Number of pages needed for n_items (0 items -> 0 pages)."""
            return n_items // per_page
        '''),
      "test_pager.py": D('''
        import pytest
        from pager import paginate, page_count

        def test_first_page():
            assert paginate(list(range(10)), 1, 3) == [0, 1, 2]

        def test_last_partial_page():
            assert paginate(list(range(10)), 4, 3) == [9]

        def test_page_count():
            assert page_count(10, 3) == 4
            assert page_count(9, 3) == 3

        def test_invalid():
            with pytest.raises(ValueError):
                paginate([1], 0, 3)
        ''')},
     None, {"pager.py": D('''
        def paginate(items, page, per_page):
            if page < 1 or per_page < 1:
                raise ValueError("page and per_page must be >= 1")
            start = (page - 1) * per_page
            return items[start:start + per_page]


        def page_count(n_items, per_page):
            return -(-n_items // per_page)
        ''')})


def check_t01(d: Path):
    vis = pytest(d, "test_pager.py")
    p, t = hidden_tests(d, "t01", D('''
        import pytest
        from pager import paginate, page_count
        def test_middle(): assert paginate(list(range(10)), 2, 3) == [3, 4, 5]
        def test_past_end(): assert paginate(list(range(10)), 9, 3) == []
        def test_exact(): assert paginate(list(range(9)), 3, 3) == [6, 7, 8]
        def test_zero_items(): assert page_count(0, 5) == 0
        def test_one_extra(): assert page_count(11, 5) == 3
        def test_bad_per_page():
            with pytest.raises(ValueError): paginate([1, 2], 1, 0)
        '''))
    tests_untouched = "assert paginate(list(range(10)), 4, 3) == [9]" in (d / "test_pager.py").read_text()
    score = (40 if vis.returncode == 0 else 0) + round(50 * p / max(t, 1)) + (10 if tests_untouched else 0)
    return score, f"visible tests {'pass' if vis.returncode == 0 else 'FAIL'}, hidden {p}/{t}, tests untouched: {tests_untouched}"


TASKS["t01"]["check"] = check_t01

# ---------------------------------------------------------------------------------------------- 2
task("t02", "Implement parse_duration from a spec",
     "Implement parse_duration in durations.py exactly as its docstring specifies, and make sure test_durations.py "
     "passes.",
     {"durations.py": D('''
        def parse_duration(text):
            """Convert a duration like "1h30m", "45s", "2d", "1h 5m 3s" or "90" to whole seconds (int).

            Units: d (days), h (hours), m (minutes), s (seconds). A bare number means seconds.
            Upper/lower case and spaces between parts are allowed. Each unit may appear at most once and
            units must be in descending order (d, h, m, s). Empty strings, unknown units, repeated or
            out-of-order units, and negative numbers raise ValueError.
            """
            raise NotImplementedError
        '''),
      "test_durations.py": D('''
        from durations import parse_duration

        def test_basic():
            assert parse_duration("1h30m") == 5400
            assert parse_duration("45s") == 45
        ''')},
     None, {"durations.py": D('''
        import re

        _UNITS = {"d": 86400, "h": 3600, "m": 60, "s": 1}

        def parse_duration(text):
            s = text.strip().lower().replace(" ", "")
            if not s:
                raise ValueError("empty duration")
            if s.isdigit():
                return int(s)
            parts = re.findall(r"(\\d+)([a-z])", s)
            if "".join(n + u for n, u in parts) != s:
                raise ValueError(f"invalid duration {text!r}")
            order = list(_UNITS)
            last = -1
            total = 0
            for n, u in parts:
                if u not in _UNITS or order.index(u) <= last:
                    raise ValueError(f"invalid duration {text!r}")
                last = order.index(u)
                total += int(n) * _UNITS[u]
            return total
        ''')})


def check_t02(d: Path):
    vis = pytest(d, "test_durations.py")
    p, t = hidden_tests(d, "t02", D('''
        import pytest
        from durations import parse_duration as p
        def test_days(): assert p("2d") == 172800
        def test_spaces(): assert p("1h 5m 3s") == 3903
        def test_bare(): assert p("90") == 90
        def test_case(): assert p("1H30M") == 5400
        def test_all(): assert p("1d2h3m4s") == 93784
        @pytest.mark.parametrize("bad", ["", "  ", "5x", "1m1h", "1h1h", "-5s", "h", "1.5h", "abc"])
        def test_invalid(bad):
            with pytest.raises(ValueError): p(bad)
        def test_type(): assert isinstance(p("1m"), int)
        '''))
    score = (20 if vis.returncode == 0 else 0) + round(80 * p / max(t, 1))
    return score, f"visible tests {'pass' if vis.returncode == 0 else 'FAIL'}, hidden {p}/{t}"


TASKS["t02"]["check"] = check_t02

# ---------------------------------------------------------------------------------------------- 3
REPORT = D('''
    def format_invoice_total(amount):
        if amount < 0:
            sign = "-"
            amount = -amount
        else:
            sign = ""
        whole = int(amount)
        cents = int(round((amount - whole) * 100))
        if cents == 100:
            whole += 1
            cents = 0
        return f"{sign}${whole:,}.{cents:02d}"


    def format_refund_total(amount):
        if amount < 0:
            sign = "-"
            amount = -amount
        else:
            sign = ""
        whole = int(amount)
        cents = int(round((amount - whole) * 100))
        if cents == 100:
            whole += 1
            cents = 0
        return f"Refund: {sign}${whole:,}.{cents:02d}"


    def format_balance(amount):
        if amount < 0:
            sign = "-"
            amount = -amount
        else:
            sign = ""
        whole = int(amount)
        cents = int(round((amount - whole) * 100))
        if cents == 100:
            whole += 1
            cents = 0
        text = f"{sign}${whole:,}.{cents:02d}"
        return text + (" (overdrawn)" if sign else "")
    ''')
task("t03", "Refactor duplicated code",
     "report.py has three functions with duplicated money-formatting logic. Refactor it to remove the duplication "
     "while keeping behavior exactly the same. test_report.py must keep passing; do not modify it.",
     {"report.py": REPORT, "test_report.py": D('''
        from report import format_invoice_total, format_refund_total, format_balance

        def test_invoice():
            assert format_invoice_total(1234.5) == "$1,234.50"

        def test_refund():
            assert format_refund_total(-3.999) == "Refund: -$4.00"

        def test_balance():
            assert format_balance(-10) == "-$10.00 (overdrawn)"
            assert format_balance(0) == "$0.00"
        ''')},
     None, {"report.py": D('''
        def _money(amount):
            sign = "-" if amount < 0 else ""
            amount = abs(amount)
            whole = int(amount)
            cents = int(round((amount - whole) * 100))
            if cents == 100:
                whole, cents = whole + 1, 0
            return sign, f"{sign}${whole:,}.{cents:02d}"


        def format_invoice_total(amount):
            return _money(amount)[1]


        def format_refund_total(amount):
            return "Refund: " + _money(amount)[1]


        def format_balance(amount):
            sign, text = _money(amount)
            return text + (" (overdrawn)" if sign else "")
        ''')})


def check_t03(d: Path):
    vis = pytest(d, "test_report.py")
    ns: dict = {}
    exec(REPORT, ns)
    samples = [0, 0.004, 0.005, 1, -1, 12.345, 999.995, 1234567.891, -0.5, -1000.999, 7.1, 10 ** 9 + 0.25]
    expected = {f: [ns[f](x) for x in samples] for f in ("format_invoice_total", "format_refund_total", "format_balance")}
    r = py("import json, report\n"
           f"s = {samples!r}\n"
           "print(json.dumps({f: [getattr(report, f)(x) for x in s] for f in "
           "('format_invoice_total', 'format_refund_total', 'format_balance')}))", d)
    try:
        got = json.loads(r.stdout)
        same = sum(got[f][i] == expected[f][i] for f in expected for i in range(len(samples)))
    except (json.JSONDecodeError, KeyError, IndexError):
        same = 0
    total = 3 * len(samples)
    src = (d / "report.py").read_text()
    dup = src.count("cents = int(round(") + src.count("int(round(")
    dedup = src.count("int(round(") <= 1 and len(src.splitlines()) < len(REPORT.splitlines()) * 0.8
    score = (25 if vis.returncode == 0 else 0) + round(50 * same / total) + (25 if dedup else 0)
    return score, (f"visible tests {'pass' if vis.returncode == 0 else 'FAIL'}, same behavior on {same}/{total} "
                   f"hidden inputs, duplication removed: {dedup} ({len(src.splitlines())} lines, rounding code x{dup // 2 or dup})")


TASKS["t03"]["check"] = check_t03

# ---------------------------------------------------------------------------------------------- 4
INVENTORY = D('''
    class OutOfStock(Exception):
        pass


    class Inventory:
        """Stock levels per SKU. Quantities are positive ints; SKUs are non-empty strings."""

        def __init__(self, max_per_sku=1000):
            self.max_per_sku = max_per_sku
            self._stock = {}

        def add(self, sku, qty):
            if not sku or qty <= 0:
                raise ValueError("sku must be non-empty and qty positive")
            new = self._stock.get(sku, 0) + qty
            if new > self.max_per_sku:
                raise ValueError("would exceed max_per_sku")
            self._stock[sku] = new
            return new

        def remove(self, sku, qty):
            if qty <= 0:
                raise ValueError("qty must be positive")
            have = self._stock.get(sku, 0)
            if qty > have:
                raise OutOfStock(sku)
            if qty == have:
                del self._stock[sku]
            else:
                self._stock[sku] = have - qty
            return have - qty

        def stock(self, sku):
            return self._stock.get(sku, 0)

        def total_units(self):
            return sum(self._stock.values())

        def low_stock(self, threshold):
            """SKUs with stock <= threshold, sorted alphabetically."""
            return sorted(s for s, q in self._stock.items() if q <= threshold)
    ''')
MUTANTS = [
    ("new > self.max_per_sku", "new >= self.max_per_sku"),
    ("if qty > have:", "if qty >= have:"),
    ("del self._stock[sku]", "self._stock[sku] = 0"),
    ("if q <= threshold", "if q < threshold"),
    ("return sorted(s for s", "return list(s for s"),
    ("if not sku or qty <= 0:", "if qty <= 0:"),
]
task("t04", "Write unit tests for a class",
     "Write thorough pytest unit tests for inventory.py in a new file test_inventory.py, covering normal behavior, "
     "edge cases and errors. Do not modify inventory.py.",
     {"inventory.py": INVENTORY}, None,
     {"test_inventory.py": D('''
        import pytest
        from inventory import Inventory, OutOfStock

        def test_add_and_stock():
            inv = Inventory(); assert inv.add("a", 3) == 3; assert inv.stock("a") == 3; assert inv.stock("x") == 0
        def test_add_limits():
            inv = Inventory(max_per_sku=5); inv.add("a", 5)
            with pytest.raises(ValueError): inv.add("a", 1)
        def test_add_invalid():
            with pytest.raises(ValueError): Inventory().add("", 1)
            with pytest.raises(ValueError): Inventory().add("a", 0)
        def test_remove():
            inv = Inventory(); inv.add("a", 5); assert inv.remove("a", 2) == 3
            with pytest.raises(OutOfStock): inv.remove("a", 4)
            assert inv.remove("a", 3) == 0; assert "a" not in inv.low_stock(10)
        def test_low_stock_sorted():
            inv = Inventory(); inv.add("b", 2); inv.add("a", 2); inv.add("c", 9)
            assert inv.low_stock(2) == ["a", "b"]; assert inv.total_units() == 13
        ''')})


def check_t04(d: Path):
    tf = d / "test_inventory.py"
    if not tf.exists():
        return 0, "no test_inventory.py"
    untouched = (d / "inventory.py").read_text() == INVENTORY
    base = pytest(d, "test_inventory.py")
    n_tests = tf.read_text().count("def test_")
    killed = 0
    if base.returncode == 0:
        for old, new in MUTANTS:
            (d / "inventory.py").write_text(INVENTORY.replace(old, new, 1))
            if pytest(d, "test_inventory.py").returncode != 0:
                killed += 1
        (d / "inventory.py").write_text(INVENTORY)
    score = (25 if base.returncode == 0 else 0) + round(65 * killed / len(MUTANTS)) + (10 if untouched else 0)
    return score, (f"{n_tests} tests, pass on original: {base.returncode == 0}, planted bugs caught {killed}/"
                   f"{len(MUTANTS)}, inventory.py untouched: {untouched}")


TASKS["t04"]["check"] = check_t04

# ---------------------------------------------------------------------------------------------- 5
task("t05", "Root-cause a bug across modules",
     "test_checkout.py fails. Find the root cause (it may not be in checkout.py) and fix it properly. Do not modify "
     "the tests.",
     {"pricing.py": D('''
        def apply_discount(price, discount_percent):
            """Price after a percentage discount, e.g. apply_discount(200, 15) -> 170.0."""
            if not 0 <= discount_percent <= 100:
                raise ValueError("discount_percent must be within 0..100")
            return round(price * (1 - discount_percent), 2)
        '''),
      "config.py": D('''
        MEMBER_DISCOUNT_PERCENT = 10
        TAX_RATE = 0.08
        '''),
      "checkout.py": D('''
        from config import MEMBER_DISCOUNT_PERCENT, TAX_RATE
        from pricing import apply_discount

        def total(prices, member=False):
            subtotal = sum(prices)
            if member:
                subtotal = apply_discount(subtotal, MEMBER_DISCOUNT_PERCENT)
            return round(subtotal * (1 + TAX_RATE), 2)
        '''),
      "test_checkout.py": D('''
        from checkout import total

        def test_regular():
            assert total([10, 20]) == 32.4

        def test_member_discount():
            assert total([100]) == 108.0
            assert total([100], member=True) == 97.2
        ''')},
     None, {"pricing.py": D('''
        def apply_discount(price, discount_percent):
            if not 0 <= discount_percent <= 100:
                raise ValueError("discount_percent must be within 0..100")
            return round(price * (1 - discount_percent / 100), 2)
        ''')})


def check_t05(d: Path):
    vis = pytest(d, "test_checkout.py")
    p, t = hidden_tests(d, "t05", D('''
        import pytest
        from pricing import apply_discount
        from checkout import total
        def test_doc_example(): assert apply_discount(200, 15) == 170.0
        def test_zero(): assert apply_discount(50, 0) == 50
        def test_full(): assert apply_discount(50, 100) == 0
        def test_range():
            with pytest.raises(ValueError): apply_discount(10, 101)
        def test_member_cart(): assert total([20, 30], member=True) == 48.6
        '''))
    config_ok = "MEMBER_DISCOUNT_PERCENT = 10" in (d / "config.py").read_text()
    tests_ok = "== 97.2" in (d / "test_checkout.py").read_text()
    score = (30 if vis.returncode == 0 else 0) + round(50 * p / max(t, 1)) + (10 if config_ok else 0) + (10 if tests_ok else 0)
    return score, (f"visible tests {'pass' if vis.returncode == 0 else 'FAIL'}, hidden {p}/{t}, config untouched: "
                   f"{config_ok}, tests untouched: {tests_ok}")


TASKS["t05"]["check"] = check_t05

# ---------------------------------------------------------------------------------------------- 6
task("t06", "Build a word-frequency CLI",
     "Create wordfreq.py. Usage: `python3 wordfreq.py FILE [-n N]`. It prints the N most common words in FILE "
     "(default N=10), one per line as `word count`. Words are compared case-insensitively (print them lowercase), "
     "consist of letters, digits and apostrophes, and everything else is a separator. Sort by count descending, "
     "then word ascending. If FILE does not exist, print an error to stderr and exit with status 1.",
     {"sample.txt": "The cat and the hat. The CAT sat!\nIt's the cat's hat, isn't it?\n"}, None,
     {"wordfreq.py": D('''
        import argparse, re, sys
        from collections import Counter
        ap = argparse.ArgumentParser(); ap.add_argument("file"); ap.add_argument("-n", type=int, default=10)
        a = ap.parse_args()
        try:
            text = open(a.file, encoding="utf-8").read()
        except OSError as e:
            print(f"error: {e}", file=sys.stderr); sys.exit(1)
        c = Counter(re.findall(r"[a-z0-9']+", text.lower()))
        for w, n in sorted(c.items(), key=lambda kv: (-kv[1], kv[0]))[:a.n]:
            print(w, n)
        ''')})


def check_t06(d: Path):
    import re
    from collections import Counter
    if not (d / "wordfreq.py").exists():
        return 0, "no wordfreq.py"
    cases = {
        "a.txt": "Hello, hello! HELLO world... world? 42 apples; 42 Apples\n",
        "b.txt": "It's a dog's life — isn't it? It's.\n\tTabs\tand\nnewlines\n",
        "c.txt": " ".join(f"w{i % 37}" for i in range(2000)) + " zeta alpha alpha zeta",
    }
    ok = 0
    total = 0
    for name, text in cases.items():
        (d / name).write_text(text)
        for n in (3, 10):
            total += 1
            c = Counter(re.findall(r"[a-z0-9']+", text.lower()))
            want = "\n".join(f"{w} {k}" for w, k in sorted(c.items(), key=lambda kv: (-kv[1], kv[0]))[:n])
            args = [name, "-n", str(n)] if n != 10 else [name]
            r = _run([sys.executable, "wordfreq.py", *args], d, 30)
            ok += r.stdout.strip() == want
        (d / name).unlink()
    r = _run([sys.executable, "wordfreq.py", "missing.txt"], d, 30)
    err_ok = r.returncode == 1 and r.stderr.strip() != "" and "Traceback" not in r.stderr
    score = round(85 * ok / total) + (15 if err_ok else 0)
    return score, f"correct output {ok}/{total} hidden runs, missing-file handling correct: {err_ok}"


TASKS["t06"]["check"] = check_t06

# ---------------------------------------------------------------------------------------------- 7
SALES = D('''
    date,region,product,quantity,unit_price
    2026-01-03,West,Widget,3,19.99
    2026-01-04,East,Gadget,1,149.50
    2026-01-04,West,Gadget,2,149.50
    2026-01-05,North,Widget,10,19.99
    2026-01-06,East,Doohickey,7,4.25
    2026-01-07,North,Gadget,1,149.50
    ''').lstrip()
HIDDEN_SALES = D('''
    date,region,product,quantity,unit_price
    2026-02-01,South,Gizmo,4,12.10
    2026-02-01,East,Gizmo,1,12.10
    2026-02-02,South,Widget,2,19.99
    2026-02-03,Central,Gadget,3,149.50
    2026-02-03,East,Widget,5,19.99
    ''').lstrip()


def expected_summary(csv_text):
    import csv
    import io
    rows = list(csv.DictReader(io.StringIO(csv_text)))
    reg, prod = {}, {}
    for r in rows:
        v = int(r["quantity"]) * float(r["unit_price"])
        reg[r["region"]] = reg.get(r["region"], 0) + v
        prod[r["product"]] = prod.get(r["product"], 0) + v
    return {"revenue_by_region": {k: round(reg[k], 2) for k in sorted(reg)},
            "total_revenue": round(sum(reg.values()), 2), "top_product": max(prod, key=prod.get)}


task("t07", "CSV to JSON sales summary",
     "Write summarize.py that reads sales.csv (in the current directory) and writes summary.json with exactly these "
     "keys: \"revenue_by_region\" (object mapping region -> total revenue, where revenue = quantity * unit_price, "
     "rounded to 2 decimals, keys sorted alphabetically), \"total_revenue\" (number, rounded to 2 decimals) and "
     "\"top_product\" (the product with the highest total revenue). Run it once to create summary.json.",
     {"sales.csv": SALES}, None,
     {"summarize.py": D('''
        import csv, json
        reg, prod = {}, {}
        with open("sales.csv", newline="") as f:
            for r in csv.DictReader(f):
                v = int(r["quantity"]) * float(r["unit_price"])
                reg[r["region"]] = reg.get(r["region"], 0) + v
                prod[r["product"]] = prod.get(r["product"], 0) + v
        json.dump({"revenue_by_region": {k: round(reg[k], 2) for k in sorted(reg)},
                   "total_revenue": round(sum(reg.values()), 2), "top_product": max(prod, key=prod.get)},
                  open("summary.json", "w"), indent=2)
        ''')})


def _summary_matches(got, want):
    try:
        same_regions = list(got["revenue_by_region"]) == list(want["revenue_by_region"]) and all(
            abs(got["revenue_by_region"][k] - v) < 0.006 for k, v in want["revenue_by_region"].items())
        return (same_regions, abs(got["total_revenue"] - want["total_revenue"]) < 0.006,
                got["top_product"] == want["top_product"], set(got) == set(want))
    except (KeyError, TypeError):
        return (False, False, False, False)


def check_t07(d: Path):
    if not (d / "summarize.py").exists():
        return 0, "no summarize.py"
    pts = 0
    notes = []
    try:
        pre = json.loads((d / "summary.json").read_text())
        produced = all(_summary_matches(pre, expected_summary(SALES)))
    except (OSError, json.JSONDecodeError):
        produced = False
    pts += 10 if produced else 0
    notes.append(f"summary.json created correctly: {produced}")
    (d / "sales.csv").write_text(HIDDEN_SALES)
    (d / "summary.json").unlink(missing_ok=True)
    _run([sys.executable, "summarize.py"], d, 30)
    try:
        got = json.loads((d / "summary.json").read_text())
        parts = _summary_matches(got, expected_summary(HIDDEN_SALES))
    except (OSError, json.JSONDecodeError):
        parts = (False, False, False, False)
    pts += 40 * parts[0] + 20 * parts[1] + 20 * parts[2] + 10 * parts[3]
    notes.append(f"hidden CSV: regions {parts[0]}, total {parts[1]}, top product {parts[2]}, exact keys {parts[3]}")
    (d / "sales.csv").write_text(SALES)
    return pts, "; ".join(notes)


TASKS["t07"]["check"] = check_t07

# ---------------------------------------------------------------------------------------------- 8
GEOMETRY = D('''
    import math


    def circle_area(radius):
        if radius < 0:
            raise ValueError("radius must be >= 0")
        return math.pi * radius ** 2


    def rect_area(width, height):
        if width < 0 or height < 0:
            raise ValueError("sides must be >= 0")
        return width * height


    def distance(p, q):
        return math.hypot(q[0] - p[0], q[1] - p[1])


    def polygon_perimeter(points):
        if len(points) < 3:
            raise ValueError("a polygon needs at least 3 points")
        return sum(distance(points[i], points[(i + 1) % len(points)]) for i in range(len(points)))


    def scale(points, factor):
        return [(x * factor, y * factor) for x, y in points]
    ''')
FUNCS = ["circle_area", "rect_area", "distance", "polygon_perimeter", "scale"]
task("t08", "Document a module",
     "Add a clear docstring to every public function in geometry.py (what it does, its arguments, what it returns, and "
     "the errors it raises), and write a README.md that explains the module and includes a short, runnable Python "
     "usage example in a ```python code block. Do not change any behavior.",
     {"geometry.py": GEOMETRY}, None, {})


def check_t08(d: Path):
    import ast
    src = (d / "geometry.py").read_text()
    try:
        tree = ast.parse(src)
    except SyntaxError:
        return 0, "geometry.py has a syntax error"
    fns = {n.name: n for n in tree.body if isinstance(n, ast.FunctionDef)}
    doc_pts = 0
    for f in FUNCS:
        ds = ast.get_docstring(fns[f]) if f in fns else None
        if ds:
            doc_pts += 1
            args = [a.arg for a in fns[f].args.args]
            doc_pts += all(a in ds for a in args) * 0.5
            raises = "raise ValueError" in ast.unparse(fns[f])
            doc_pts += (("ValueError" in ds) if raises else True) * 0.5
    doc_score = 40 * doc_pts / (2 * len(FUNCS))
    r = py("import math, geometry as g\n"
           "assert abs(g.circle_area(2) - math.pi * 4) < 1e-9 and g.rect_area(2, 3) == 6 and g.distance((0, 0), (3, 4)) == 5\n"
           "assert abs(g.polygon_perimeter([(0, 0), (1, 0), (1, 1), (0, 1)]) - 4) < 1e-9 and g.scale([(1, 2)], 2) == [(2, 4)]\n"
           "for bad in (lambda: g.circle_area(-1), lambda: g.rect_area(-1, 1), lambda: g.polygon_perimeter([(0, 0)])):\n"
           "    try:\n        bad()\n    except ValueError:\n        pass\n    else:\n        raise SystemExit(1)\n", d)
    behavior = r.returncode == 0
    readme = d / "README.md"
    rd = readme.read_text() if readme.exists() else ""
    mentioned = sum(f in rd for f in FUNCS)
    import re
    blocks = re.findall(r"```python\n(.*?)```", rd, re.S)
    example_runs = bool(blocks) and py(blocks[0], d, timeout=30).returncode == 0
    score = round(doc_score) + (20 if behavior else 0) + round(20 * mentioned / len(FUNCS)) + (20 if example_runs else 0)
    return score, (f"docstrings {doc_pts}/{2 * len(FUNCS)} points, behavior unchanged: {behavior}, README mentions "
                   f"{mentioned}/{len(FUNCS)} functions, README example runs: {example_runs}")


TASKS["t08"]["check"] = check_t08
TASKS["t08"]["reference"] = {"geometry.py": GEOMETRY.replace(
    "def circle_area(radius):\n", 'def circle_area(radius):\n    """Area of a circle. Args: radius. Returns float. Raises ValueError if radius < 0."""\n').replace(
    "def rect_area(width, height):\n", 'def rect_area(width, height):\n    """Area of a rectangle. Args: width, height. Returns their product. Raises ValueError if either is < 0."""\n').replace(
    "def distance(p, q):\n", 'def distance(p, q):\n    """Euclidean distance between points p and q (x, y tuples). Returns float."""\n').replace(
    "def polygon_perimeter(points):\n", 'def polygon_perimeter(points):\n    """Perimeter of a closed polygon. Args: points. Raises ValueError with fewer than 3 points."""\n').replace(
    "def scale(points, factor):\n", 'def scale(points, factor):\n    """Multiply every point by factor. Args: points, factor. Returns a new list."""\n'),
    "README.md": "# geometry\n\ncircle_area, rect_area, distance, polygon_perimeter, scale.\n\n```python\nimport geometry\n"
                 "print(geometry.rect_area(2, 3), geometry.distance((0, 0), (3, 4)))\n"
                 "print(geometry.polygon_perimeter(geometry.scale([(0, 0), (1, 0), (0, 1)], 2)), geometry.circle_area(1))\n```\n"}

# ---------------------------------------------------------------------------------------------- 9
DEDUPE = D('''
    def find_duplicates(items):
        """Values that occur more than once, each listed once, in the order their second occurrence appears."""
        result = []
        for i, x in enumerate(items):
            if x in items[:i] and x not in result:
                result.append(x)
        return result
    ''')
task("t09", "Speed up a slow function",
     "find_duplicates in dedupe.py is far too slow for large inputs. Make it fast enough to handle 200,000 items in "
     "well under a second, without changing its results (including their order). test_dedupe.py must keep passing.",
     {"dedupe.py": DEDUPE, "test_dedupe.py": D('''
        from dedupe import find_duplicates

        def test_basic():
            assert find_duplicates([1, 2, 3, 2, 1, 2]) == [2, 1]

        def test_none():
            assert find_duplicates(["a", "b"]) == []
        ''')}, None,
     {"dedupe.py": D('''
        def find_duplicates(items):
            seen, dup, result = set(), set(), []
            for x in items:
                if x in seen:
                    if x not in dup:
                        dup.add(x)
                        result.append(x)
                else:
                    seen.add(x)
            return result
        ''')})


def check_t09(d: Path):
    vis = pytest(d, "test_dedupe.py")
    ns: dict = {}
    exec(DEDUPE, ns)
    import random
    rnd = random.Random(7)
    cases = [[rnd.randint(0, 50) for _ in range(300)], list("mississippi"), [3, 3, 3], [], [1, 2, 1, 2, 3, 3, 1]]
    want = [ns["find_duplicates"](c) for c in cases]
    r = py("import json, dedupe\n" f"cases = {cases!r}\n" "print(json.dumps([dedupe.find_duplicates(c) for c in cases]))", d)
    try:
        same = json.loads(r.stdout) == want
    except json.JSONDecodeError:
        same = False
    t = py("import random, time, dedupe\nr = random.Random(1)\nxs = [r.randint(0, 150000) for _ in range(200000)]\n"
           "t = time.perf_counter(); dedupe.find_duplicates(xs); print(time.perf_counter() - t)", d, timeout=20)
    try:
        secs = float(t.stdout.strip())
    except ValueError:
        secs = None
    speed = 0 if secs is None else (40 if secs < 0.25 else 25 if secs < 1 else 0)
    score = (20 if vis.returncode == 0 else 0) + (40 if same else 0) + speed
    return score, (f"visible tests {'pass' if vis.returncode == 0 else 'FAIL'}, identical results: {same}, "
                   f"200k items: {'timeout/>20 s' if secs is None else f'{secs * 1000:.0f} ms'}")


TASKS["t09"]["check"] = check_t09

# ---------------------------------------------------------------------------------------------- 10
USERS = D('''
    import hashlib
    import sqlite3


    def connect(path=":memory:"):
        db = sqlite3.connect(path)
        db.execute("CREATE TABLE IF NOT EXISTS users (name TEXT PRIMARY KEY, password_hash TEXT, role TEXT)")
        return db


    def _hash(password):
        return hashlib.sha256(password.encode()).hexdigest()


    def add_user(db, name, password, role="user"):
        db.execute(f"INSERT INTO users VALUES ('{name}', '{_hash(password)}', '{role}')")
        db.commit()


    def find_user(db, name):
        row = db.execute(f"SELECT name, role FROM users WHERE name = '{name}'").fetchone()
        return {"name": row[0], "role": row[1]} if row else None


    def login(db, name, password):
        row = db.execute(f"SELECT 1 FROM users WHERE name = '{name}' AND password_hash = '{_hash(password)}'").fetchone()
        return row is not None
    ''')
task("t10", "Fix SQL injection vulnerabilities",
     "Review users.py for security vulnerabilities and fix every one you find. Keep test_users.py passing and keep the "
     "same function signatures.",
     {"users.py": USERS, "test_users.py": D('''
        from users import connect, add_user, find_user, login

        def test_roundtrip():
            db = connect()
            add_user(db, "alice", "s3cret", role="admin")
            assert find_user(db, "alice") == {"name": "alice", "role": "admin"}
            assert login(db, "alice", "s3cret")
            assert not login(db, "alice", "wrong")
            assert find_user(db, "bob") is None
        ''')}, None,
     {"users.py": USERS.replace("""f"INSERT INTO users VALUES ('{name}', '{_hash(password)}', '{role}')\"""",
                                '"INSERT INTO users VALUES (?, ?, ?)", (name, _hash(password), role)')
      .replace("""f"SELECT name, role FROM users WHERE name = '{name}'\"""", '"SELECT name, role FROM users WHERE name = ?", (name,)')
      .replace("""f"SELECT 1 FROM users WHERE name = '{name}' AND password_hash = '{_hash(password)}'\"""",
               '"SELECT 1 FROM users WHERE name = ? AND password_hash = ?", (name, _hash(password))')})


def check_t10(d: Path):
    vis = pytest(d, "test_users.py")
    attack = D('''
        import json, users
        db = users.connect(); users.add_user(db, "alice", "s3cret", role="admin")
        res = {}
        res["login_bypass"] = users.login(db, "alice' --", "anything")
        res["login_or"] = users.login(db, "x' OR '1'='1' --", "y")
        r = users.find_user(db, "nobody' OR '1'='1")
        res["find_or"] = r is not None
        try:
            users.add_user(db, "mallory", "pw", role="user'); DROP TABLE users; --")
        except Exception:
            pass
        try:
            res["insert_role"] = (users.find_user(db, "mallory") or {}).get("role") not in (None, "user'); DROP TABLE users; --")
            res["table_alive"] = users.find_user(db, "alice") is None
        except Exception:
            res["insert_role"] = True; res["table_alive"] = True
        try:
            users.add_user(db, "o'brien", "pw"); res["quote_name"] = users.find_user(db, "o'brien") is None
        except Exception:
            res["quote_name"] = True
        print(json.dumps(res))
        ''')
    r = py(attack, d)
    try:
        res = json.loads(r.stdout)
    except json.JSONDecodeError:
        return (30 if vis.returncode == 0 else 0), f"attack script failed: {r.stderr[-200:]}"
    blocked = [k for k, v in res.items() if not v]
    score = (30 if vis.returncode == 0 else 0) + round(70 * len(blocked) / len(res))
    return score, (f"visible tests {'pass' if vis.returncode == 0 else 'FAIL'}, attacks blocked "
                   f"{len(blocked)}/{len(res)}" + (f" (still vulnerable: {', '.join(k for k in res if res[k])})"
                                                   if len(blocked) < len(res) else ""))


TASKS["t10"]["check"] = check_t10


def materialize(tid: str, d: Path) -> None:
    d.mkdir(parents=True, exist_ok=True)
    for name, body in TASKS[tid]["files"].items():
        (d / name).write_text(body)


if __name__ == "__main__":  # self-test: original files score low, reference solutions score 100
    import shutil
    import tempfile
    for tid, t in TASKS.items():
        base = Path(tempfile.mkdtemp())
        materialize(tid, base / "orig")
        o, on = t["check"](base / "orig")
        materialize(tid, base / "ref")
        for name, body in t["reference"].items():
            (base / "ref" / name).write_text(body)
        if tid == "t07":
            subprocess.run([sys.executable, "summarize.py"], cwd=base / "ref", timeout=30)
        r, rn = t["check"](base / "ref")
        print(f"{tid} {t['title']:40} original {o:3}   reference {r:3}   | {rn}")
        shutil.rmtree(base)
