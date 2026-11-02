# tutor.py
#
# SQL tutor: read a lesson, run its example code in DuckDB, and see the results.
#
#   python sql_tutor/tutor.py                  list the lessons
#   python sql_tutor/tutor.py 9                show lesson 9 with live results   (or a name: dedup)
#   python sql_tutor/tutor.py 9 --no-run       show the lesson text only
#   python sql_tutor/tutor.py --check          run every lesson, report PASS / FAIL
#
# Every lesson is a Markdown file in lessons/ with four sections: theory, real-world case,
# popular usage and example code. Only the code blocks under "## 4. Example code" are run:
#   ```sql      executed in DuckDB, the result of the last statement is printed
#   ```python   executed with these names available: con, duckdb, run_sql_file, build_silver, HERE
#   anything else (```postgres, ```text, ...) is shown but never run.
# Each lesson gets a fresh in-memory DuckDB, so lessons never affect each other.

import argparse
import contextlib
import io
import os
import sys
import traceback
from pathlib import Path

import duckdb

HERE = Path(__file__).resolve().parent
LESSONS_DIR = HERE / "lessons"
PROJECT_SQL = HERE.parent / "flatten_datasets_metadata.sql"
MAX_ROWS = 15
MAX_WIDTH = 64


# ------------------------------------------------------------------ finding and parsing lessons


def lesson_files() -> list[Path]:
    return sorted(LESSONS_DIR.glob("[0-9][0-9]_*.md"))


def lesson_title(path: Path) -> str:
    return path.read_text(encoding="utf-8").splitlines()[0].lstrip("# ").strip()


def find_lesson(query: str) -> Path:
    files = lesson_files()
    if query.isdigit():
        hits = [f for f in files if int(f.name[:2]) == int(query)]
    else:
        hits = [f for f in files if query.lower() in f.stem.lower()]
    if len(hits) != 1:
        names = ", ".join(f.stem for f in hits) or "none"
        sys.exit(f"Lesson {query!r} is ambiguous or unknown (matches: {names}). Run without arguments to list lessons.")
    return hits[0]


def parse(text: str) -> list[tuple]:
    """Split a lesson into ("text", str) and ("code", lang, body, in_example) segments."""
    segments, buf, code = [], [], []
    in_code, lang, in_example = False, "", False
    for line in text.splitlines():
        if not in_code:
            if line.startswith("```"):
                if buf:
                    segments.append(("text", "\n".join(buf)))
                    buf = []
                in_code, lang, code = True, line[3:].strip(), []
            else:
                if line.startswith("## 4"):
                    in_example = True
                buf.append(line)
        elif line.startswith("```"):
            segments.append(("code", lang, "\n".join(code), in_example))
            in_code = False
        else:
            code.append(line)
    if buf:
        segments.append(("text", "\n".join(buf)))
    return segments


# ------------------------------------------------------------------ running code


def format_result(rel, max_rows: int = MAX_ROWS, max_width: int = MAX_WIDTH) -> str | None:
    """Render a DuckDB relation as a plain ASCII table (everything cast to text inside DuckDB)."""
    cols = list(rel.columns)
    if cols == ["Count"]:  # DDL / INSERT bookkeeping, nothing to show
        return None
    as_text = rel.project(", ".join(f'CAST("{c}" AS VARCHAR) AS "{c}"' for c in cols))
    rows = as_text.fetchall()
    shown = [["NULL" if v is None else str(v).replace("\n", " ") for v in r] for r in rows[:max_rows]]
    shown = [[v if len(v) <= max_width else v[: max_width - 3] + "..." for v in r] for r in shown]
    widths = [max([len(c)] + [len(r[i]) for r in shown]) for i, c in enumerate(cols)]
    lines = [" | ".join(c.ljust(w) for c, w in zip(cols, widths)),
             "-+-".join("-" * w for w in widths)]
    lines += [" | ".join(v.ljust(w) for v, w in zip(r, widths)) for r in shown]
    footer = f"({len(rows)} row{'' if len(rows) == 1 else 's'}" + (f", first {max_rows} shown)" if len(rows) > max_rows else ")")
    return "\n".join(lines + [footer])


def make_namespace(con) -> dict:
    def run_sql_file(name: str) -> None:
        con.execute((HERE / name).read_text(encoding="utf-8"))

    def build_silver() -> None:
        """Run the project's real flatten_datasets_metadata.sql on the sample files."""
        files = sorted((HERE / "sample_data").glob("*/*/entity=dataset_metadata/*/*.jsonl"))
        file_list = "[" + ", ".join("'" + f.as_posix().replace("'", "''") + "'" for f in files) + "]"
        con.execute(PROJECT_SQL.read_text(encoding="utf-8").replace("{{RAW_FILES}}", file_list))

    return {"con": con, "duckdb": duckdb, "run_sql_file": run_sql_file, "build_silver": build_silver, "HERE": HERE}


def open_lesson_db():
    """A fresh in-memory DuckDB plus the namespace the python example blocks run in."""
    os.chdir(HERE)  # the sample data and helper files are addressed relative to this folder
    con = duckdb.connect()
    con.execute("SET TimeZone = 'UTC'")
    return con, make_namespace(con)


def run_block(con, ns: dict, lang: str, body: str, name: str) -> str | None:
    """Execute one example block (sql or python) and return the text to show under it, or None."""
    if lang == "sql":
        rel = con.sql(body)
        return format_result(rel) if rel is not None else None
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        exec(compile(body, f"<{name}>", "exec"), ns)
    return buf.getvalue().rstrip() or None


def indent(text: str, spaces: int) -> str:
    pad = " " * spaces
    return "\n".join(pad + line if line else line for line in text.splitlines())


def run_lesson(path: Path, show: bool = True, run: bool = True) -> None:
    """Show and/or run one lesson. Raises if any example block fails."""
    con, ns = open_lesson_db()

    for seg in parse(path.read_text(encoding="utf-8")):
        if seg[0] == "text":
            if show:
                print(seg[1])
            continue
        _, lang, body, in_example = seg
        runnable = run and in_example and lang in ("sql", "python")
        if show:
            label = "" if runnable or not in_example else f"   (not run: {lang or 'text'})"
            print(indent(body, 4) + (f"\n{label}" if label else ""))
        if not runnable:
            continue
        try:
            out = run_block(con, ns, lang, body, path.stem)
        except Exception as e:
            if show:
                print(indent(f"ERROR: {str(e).splitlines()[0]}", 4))
            raise RuntimeError(f"{path.stem}: this block failed:\n{body}\n--> {e}") from e
        if show and out:
            print("\n  Result:\n" + indent(out, 4) + "\n")
    con.close()


def check_all() -> int:
    failed = 0
    for path in lesson_files():
        try:
            with contextlib.redirect_stdout(io.StringIO()):
                run_lesson(path, show=False)
            print(f"PASS  {path.stem}")
        except Exception:
            failed += 1
            print(f"FAIL  {path.stem}")
            print(indent(traceback.format_exc().splitlines()[-1], 6))
    print(f"\n{len(lesson_files()) - failed} passed, {failed} failed")
    return 1 if failed else 0


# ------------------------------------------------------------------ command line


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    parser = argparse.ArgumentParser(description="Interactive SQL tutor built on DuckDB.")
    parser.add_argument("lesson", nargs="?", help="lesson number (9) or part of its name (dedup)")
    parser.add_argument("--no-run", action="store_true", help="show the lesson text without running the code")
    parser.add_argument("--check", action="store_true", help="run every lesson and report PASS / FAIL")
    args = parser.parse_args()

    from sample_data import ensure_sample_data
    ensure_sample_data()

    if args.check:
        return check_all()
    if not args.lesson:
        print("SQL tutor lessons (python sql_tutor/tutor.py <number or name>):\n")
        for f in lesson_files():
            print(f"  {f.name[:2]}  {lesson_title(f).split(' - ', 1)[-1]}")
        return 0
    try:
        run_lesson(find_lesson(args.lesson), show=True, run=not args.no_run)
    except RuntimeError as e:
        print(f"\n{e}")
        return 1
    return 0


if __name__ == "__main__":
    sys.path.insert(0, str(HERE))
    sys.exit(main())
