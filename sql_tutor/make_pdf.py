# make_pdf.py
#
# Builds SQL_Tutor.pdf: all lessons, with every example's live result, as one printable document
# (cover, clickable contents with page numbers, PDF bookmarks, page-number footer).
#
#   python sql_tutor/make_pdf.py
#
# How it works: each lesson is executed with the same code the tutor uses (tutor.run_block), the
# Markdown is converted to HTML, and an installed Chrome or Edge prints the HTML to PDF. No extra
# Python packages are needed. If pdftotext is on the PATH, a second pass fills in the page numbers
# of the contents page. Re-run this script after editing a lesson.

import datetime
import html
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import duckdb

import tutor

HERE = tutor.HERE
OUT = HERE / "SQL_Tutor.pdf"
README = HERE / "README.md"

# (group title, first lesson, last lesson) for the contents page
GROUPS = [
    ("Foundations", 1, 6),
    ("Query building blocks", 7, 9),
    ("Semi-structured data", 10, 15),
    ("Types, time and reuse", 16, 18),
    ("Pipeline engineering", 19, 25),
]
README_SECTIONS = ["Quick start", "Suggested path", "Map from the topic list to lessons",
                   "The sample data", "How the code blocks run", "Notes"]

# ------------------------------------------------------------------ inline Markdown

CODE_SPAN = re.compile(r"(?<!`)(`+)(?!`)(.+?)(?<!`)\1(?!`)")


def inline(text: str, in_table: bool = False) -> str:
    """Inline Markdown (code spans, bold, italic) to HTML. Everything else is escaped.

    In table cells, short code spans are kept in one piece so a narrow column does not split them mid-token."""
    spans: list[str] = []

    def stash(m):
        content = m.group(2)
        if len(content) > 2 and content.startswith(" ") and content.endswith(" "):
            content = content[1:-1]
        spans.append(content)
        return f"\x00{len(spans) - 1}\x00"

    t = CODE_SPAN.sub(stash, text)
    t = html.escape(t, quote=False)
    t = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", t)
    t = re.sub(r"(?<![\w*])\*(?!\s)([^*\n]+?)(?<!\s)\*(?![\w*])", r"<em>\1</em>", t)
    def code_html(m):
        content = spans[int(m.group(1))]
        cls = ' class="nw"' if in_table and len(content) <= 26 else ""
        return f"<code{cls}>{html.escape(content, quote=False)}</code>"

    return re.sub(r"\x00(\d+)\x00", code_html, t)


# ------------------------------------------------------------------ block Markdown


def split_row(row: str) -> list[str]:
    row = row.strip()
    row = row[1:] if row.startswith("|") else row
    row = row[:-1] if row.endswith("|") else row
    cells, cur, in_code = [], [], False
    for ch in row:
        if ch == "`":
            in_code = not in_code
        if ch == "|" and not in_code:
            cells.append("".join(cur).strip())
            cur = []
        else:
            cur.append(ch)
    cells.append("".join(cur).strip())
    return cells


def table_html(rows: list[str]) -> str:
    head = split_row(rows[0])
    body = [split_row(r) for r in rows[1:]]
    if body and all(re.fullmatch(r":?-{3,}:?", c) for c in body[0]):
        body = body[1:]
    th = "".join(f"<th>{inline(c, True)}</th>" for c in head)
    trs = "".join("<tr>" + "".join(f"<td>{inline(c, True)}</td>" for c in r) + "</tr>" for r in body)
    return f"<table><thead><tr>{th}</tr></thead><tbody>{trs}</tbody></table>"


def paragraph_html(text: str) -> str:
    if text.startswith("Level:"):
        chips = "".join(f"<span>{inline(p.strip())}</span>" for p in text.split(" | "))
        return f'<div class="meta">{chips}</div>'
    return f"<p>{inline(text)}</p>"


def md_blocks(text: str) -> str:
    """Block-level Markdown (headings, paragraphs, lists, tables) to HTML."""
    lines = text.split("\n")
    out: list[str] = []
    para: list[str] = []
    i = 0

    def flush():
        if para:
            out.append(paragraph_html(" ".join(para).strip()))
            para.clear()

    while i < len(lines):
        line = lines[i]
        s = line.strip()
        if not s:
            flush()
            i += 1
            continue
        m = re.match(r"(#{1,3})\s+(.*)", line)
        if m:
            flush()
            out.append(f"<h{len(m.group(1))}>{inline(m.group(2))}</h{len(m.group(1))}>")
            i += 1
            continue
        if s.startswith("|"):
            flush()
            rows = []
            while i < len(lines) and lines[i].strip().startswith("|"):
                rows.append(lines[i])
                i += 1
            out.append(table_html(rows))
            continue
        bullet, numbered = re.match(r"- ", line), re.match(r"\d+\. ", line)
        if bullet or numbered:
            flush()
            tag, marker = ("ul", r"- ") if bullet else ("ol", r"\d+\. ")
            items: list[list[str]] = []
            while i < len(lines):
                cur = lines[i]
                mm = re.match(marker, cur)
                if mm:
                    items.append([cur[mm.end():].strip()])
                    i += 1
                elif cur.startswith("  ") and cur.strip() and items:
                    items[-1].append(cur.strip())
                    i += 1
                else:
                    break
            out.append(f"<{tag}>" + "".join(f"<li>{inline(' '.join(it))}</li>" for it in items) + f"</{tag}>")
            continue
        para.append(s)
        i += 1
    flush()
    return "\n".join(out)


# ------------------------------------------------------------------ code and results

SQL_KEYWORDS = (
    "SELECT FROM WHERE GROUP BY ORDER HAVING JOIN LEFT RIGHT FULL INNER CROSS ON USING AS WITH RECURSIVE "
    "UNION ALL INTERSECT EXCEPT DISTINCT CASE WHEN THEN ELSE END AND OR NOT NULL IS IN EXISTS BETWEEN LIKE ILIKE "
    "LIMIT OFFSET INSERT INTO VALUES UPDATE SET DELETE CREATE REPLACE TABLE TEMP TEMPORARY VIEW MACRO DROP ALTER "
    "RENAME TO PRIMARY KEY OVER PARTITION ROWS PRECEDING FOLLOWING CURRENT ROW UNBOUNDED WINDOW QUALIFY FILTER "
    "ORDINALITY UNNEST ANTI SEMI LATERAL ROLLUP CUBE GROUPING SETS BEGIN COMMIT ROLLBACK MERGE MATCHED CONFLICT "
    "DO NOTHING PIVOT UNPIVOT INTERVAL DATE TIMESTAMP TIMESTAMPTZ TRUE FALSE ASC DESC NULLS FIRST LAST COPY "
    "FORMAT DESCRIBE EXPLAIN NAME VALUE BY VARCHAR INTEGER BIGINT BOOLEAN DECIMAL DOUBLE JSON STRUCT DAY"
).split()
PY_KEYWORDS = ("def class return if else elif for in while try except finally raise import from as with lambda "
               "pass and or not is True False None print").split()

SQL_TOKENS = re.compile(
    r"(?P<cmt>--[^\n]*)|(?P<str>'(?:[^']|'')*')|(?P<num>\b\d+(?:\.\d+)?\b)|(?P<kw>\b(?:" + "|".join(SQL_KEYWORDS) + r")\b)")
PY_TOKENS = re.compile(
    r"(?P<cmt>#[^\n]*)|(?P<str>\"\"\"[\s\S]*?\"\"\"|'''[\s\S]*?'''|\"(?:\\.|[^\"\\\n])*\"|'(?:\\.|[^'\\\n])*')"
    r"|(?P<num>\b\d+(?:\.\d+)?\b)|(?P<kw>\b(?:" + "|".join(PY_KEYWORDS) + r")\b)")


def highlight(code: str, lang: str) -> str:
    pattern = {"sql": SQL_TOKENS, "postgres": SQL_TOKENS, "python": PY_TOKENS}.get(lang)
    if pattern is None:
        return html.escape(code, quote=False)
    out, pos = [], 0
    for m in pattern.finditer(code):
        out.append(html.escape(code[pos:m.start()], quote=False))
        out.append(f'<span class="{m.lastgroup}">{html.escape(m.group(), quote=False)}</span>')
        pos = m.end()
    out.append(html.escape(code[pos:], quote=False))
    return "".join(out)


def fit_class(text: str) -> str:
    """Pick a font size class so the longest line fits the page width (about 108 characters at 8 pt)."""
    longest = max((len(line) for line in text.split("\n")), default=0)
    return "f8" if longest <= 108 else "f7" if longest <= 124 else "f64"


LABELS = {"sql": "SQL", "python": "Python", "postgres": "PostgreSQL (shown, not run)",
          "bash": "Terminal", "sh": "Terminal", "shell": "Terminal"}


def code_box(lang: str, body: str, in_example: bool, out: str | None) -> str:
    fit = fit_class(body)
    if not in_example and lang not in ("bash", "sh", "shell"):
        return f'<pre class="plain {fit}">{highlight(body, "")}</pre>'
    tag = LABELS.get(lang, lang or "Text")
    box = (f'<div class="code"><div class="tag">{html.escape(tag)}</div>'
           f'<pre class="{fit}">{highlight(body, lang)}</pre></div>')
    if out:
        box += (f'<div class="result"><div class="tag">Result</div>'
                f'<pre class="{fit_class(out)}">{html.escape(out, quote=False)}</pre></div>')
    return f'<div class="example">{box}</div>'


def render_segments(text: str, con=None, ns=None, name: str = "") -> str:
    """Convert Markdown (with fenced code) to HTML; run example blocks when a connection is given."""
    parts = []
    for seg in tutor.parse(text):
        if seg[0] == "text":
            parts.append(md_blocks(seg[1]))
            continue
        _, lang, body, in_example = seg
        out = None
        if con is not None and in_example and lang in ("sql", "python"):
            out = tutor.run_block(con, ns, lang, body, name)
        parts.append(code_box(lang, body, in_example, out))
    return "\n".join(parts)


# ------------------------------------------------------------------ document parts


def render_lesson(path: Path) -> dict:
    text = path.read_text(encoding="utf-8")
    first, _, rest = text.partition("\n")
    m = re.match(r"# (\d\d) - (.*)", first)
    number, title = int(m.group(1)), m.group(2).strip()
    level = re.search(r"Level: (.*?) \|", rest).group(1)
    con, ns = tutor.open_lesson_db()
    try:
        body = render_segments(rest, con, ns, path.stem)
    finally:
        con.close()
    section = (f'<section class="lesson" id="lesson-{number:02d}"><div class="eyebrow">LESSON {number:02d}</div>'
               f"<h1>{inline(title)}</h1>\n{body}</section>")
    return {"number": number, "title": title, "level": level, "html": section}


def render_about() -> str:
    text = README.read_text(encoding="utf-8")
    intro, *sections = re.split(r"\n## ", text)
    intro = intro.split("\n", 1)[1]  # drop the "# title" line
    body = [md_blocks(intro)]
    for sec in sections:
        title, _, rest = sec.partition("\n")
        if title.strip() in README_SECTIONS:
            body.append(f"<h2>{inline(title.strip())}</h2>\n{render_segments(rest)}")
    return ('<section class="chapter"><div class="eyebrow">ABOUT</div><h1>About this module</h1>\n'
            + "\n".join(body) + "</section>")


def cover(count: int) -> str:
    return f"""<section class="cover">
<div class="cover-inner">
<div class="cover-kicker">Dataverse ELT project</div>
<h1>SQL Tutor</h1>
<div class="cover-sub">Advanced SQL for the bronze-to-silver pipeline, and the theory behind it</div>
<ul>
<li>{count} lessons on DuckDB SQL</li>
<li>Theory, a real-world case, popular usage and runnable examples in every lesson</li>
<li>Every example shows its real result</li>
</ul>
<div class="cover-foot">Generated {datetime.date.today().isoformat()} &nbsp;|&nbsp; results from DuckDB {duckdb.__version__}</div>
</div>
</section>"""


def contents(lessons: list[dict], pages: dict[int, int]) -> str:
    rows = []
    for group, lo, hi in GROUPS:
        rows.append(f"<h3>{html.escape(group)}</h3>")
        for les in lessons:
            if lo <= les["number"] <= hi:
                n = les["number"]
                page = pages.get(n, "")
                rows.append(
                    f'<a class="toc-row" href="#lesson-{n:02d}"><span class="n">{n:02d}</span>'
                    f'<span class="t">{inline(les["title"])}</span><span class="leader"></span>'
                    f'<span class="lv">{html.escape(les["level"])}</span><span class="pg">{page}</span></a>')
    return ('<section class="chapter toc"><div class="eyebrow">CONTENTS</div><h1>Lessons</h1>'
            '<p class="note">Click a title to jump to it. The PDF viewer\'s bookmarks list every lesson and section.</p>'
            + "\n".join(rows) + "</section>")


CSS = r"""
@page { size: A4; margin: 18mm 16mm 20mm 16mm;
  @bottom-left  { content: "SQL Tutor - Dataverse ELT project"; font: 8pt "Segoe UI", Arial, sans-serif; color: #8a94a6; }
  @bottom-right { content: counter(page); font: 8pt "Segoe UI", Arial, sans-serif; color: #8a94a6; } }
@page :first { margin: 0; @bottom-left { content: none; } @bottom-right { content: none; } }
* { box-sizing: border-box; }
html { -webkit-print-color-adjust: exact; print-color-adjust: exact; }
body { margin: 0; font: 10pt/1.5 "Segoe UI", "Helvetica Neue", Arial, sans-serif; color: #1f2937; }
a { color: inherit; text-decoration: none; }
p { margin: 0 0 7pt; }
ul, ol { margin: 0 0 8pt; padding-left: 18pt; }
li { margin: 0 0 2.5pt; }
strong { color: #111827; }

.cover { width: 210mm; height: 297mm; background: #12263f; color: #fff; break-after: page; display: flex; align-items: center; }
.cover-inner { padding: 0 24mm; }
.cover-kicker { font-size: 12pt; color: #8fb4e3; margin-bottom: 10pt; }
.cover h1 { font-size: 54pt; line-height: 1.05; margin: 0 0 14pt; font-weight: 700; color: #fff; }
.cover-sub { font-size: 16pt; line-height: 1.35; color: #dbe7f7; max-width: 130mm; margin-bottom: 30pt; }
.cover ul { list-style: none; padding: 0; font-size: 11.5pt; color: #c3d4ec; }
.cover li { margin-bottom: 7pt; padding-left: 14pt; position: relative; }
.cover li::before { content: ""; position: absolute; left: 0; top: 7pt; width: 5pt; height: 5pt; background: #5aa2ff; border-radius: 50%; }
.cover-foot { margin-top: 60pt; font-size: 9.5pt; color: #8fa6c4; }

.chapter, .lesson { break-before: page; }
.eyebrow { font-size: 9pt; font-weight: 700; color: #1d5fb8; margin-bottom: 2pt; }
h1 { font-size: 22pt; line-height: 1.2; margin: 0 0 8pt; color: #0f2a4a; break-after: avoid; }
h2 { font-size: 13.5pt; margin: 18pt 0 7pt; padding-bottom: 3pt; border-bottom: 1px solid #d6dde8; color: #0f2a4a; break-after: avoid; }
h3 { font-size: 11pt; margin: 12pt 0 5pt; color: #0f2a4a; break-after: avoid; }
.note { color: #5b6678; font-size: 9pt; }

.meta { display: flex; flex-wrap: wrap; gap: 4pt; margin: 0 0 6pt; }
.meta span { font-size: 8.3pt; background: #eef2f7; color: #475569; padding: 1.5pt 7pt; border-radius: 9pt; }
code { font: 0.9em Consolas, "Cascadia Mono", monospace; background: #f0f3f8; padding: 0 2.5pt; border-radius: 2pt; overflow-wrap: anywhere; }
code.nw { white-space: nowrap; }

table { width: 100%; border-collapse: collapse; margin: 3pt 0 10pt; font-size: 8.8pt; line-height: 1.4; }
th { background: #eaf0f7; text-align: left; font-weight: 600; color: #0f2a4a; }
th, td { border: 1px solid #d6dde8; padding: 3pt 6pt; vertical-align: top; overflow-wrap: anywhere; }
tr { break-inside: avoid; }

.example { break-inside: avoid; margin: 6pt 0 10pt; }
.code, .result { border: 1px solid #d3dbe7; border-radius: 4pt; overflow: hidden; }
.code { background: #f7f9fc; }
.result { margin-top: 3pt; background: #fff; border-color: #bfdcd6; }
.tag { font: 600 7.3pt "Segoe UI", Arial, sans-serif; padding: 1.5pt 8pt; color: #475569; background: #e8edf4; }
.result .tag { background: #def0ec; color: #1d5c55; }
pre { margin: 0; padding: 5pt 8pt; font-family: Consolas, "Cascadia Mono", monospace; line-height: 1.38; white-space: pre-wrap; overflow-wrap: anywhere; color: #111827; }
pre.plain { margin: 4pt 0 9pt; background: #f4f6fa; border-left: 3px solid #c3cde0; border-radius: 3pt; break-inside: avoid; }
.f8 { font-size: 8pt; } .f7 { font-size: 7pt; } .f64 { font-size: 6.4pt; }
.kw { color: #1d4ed8; font-weight: 600; } .str { color: #15803d; } .num { color: #b45309; } .cmt { color: #6b7280; font-style: italic; }

.toc h3 { margin-top: 14pt; color: #1d5fb8; border-bottom: 1px solid #e3e8f0; padding-bottom: 2pt; }
.toc-row { display: flex; align-items: baseline; padding: 2.4pt 0; font-size: 9.6pt; break-inside: avoid; }
.toc-row .n { width: 24pt; font-weight: 700; color: #1d5fb8; }
.toc-row .t { flex: 0 1 auto; }
.toc-row .leader { flex: 1; border-bottom: 1px dotted #b8c2d1; margin: 0 6pt; transform: translateY(-2pt); }
.toc-row .lv { font-size: 8pt; color: #6b7686; margin-right: 8pt; }
.toc-row .pg { width: 22pt; text-align: right; font-weight: 600; }
"""


def assemble(lessons: list[dict], about: str, pages: dict[int, int]) -> str:
    return (f'<!doctype html><html lang="en"><head><meta charset="utf-8"><title>SQL Tutor - Dataverse ELT project</title>'
            f"<style>{CSS}</style></head><body>\n{cover(len(lessons))}\n{contents(lessons, pages)}\n{about}\n"
            + "\n".join(les["html"] for les in lessons) + "\n</body></html>")


# ------------------------------------------------------------------ printing


def find_browser() -> str:
    candidates = [
        r"C:\Program Files\Google\Chrome\Application\chrome.exe",
        r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
        r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
        r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
        shutil.which("chrome"), shutil.which("msedge"), shutil.which("google-chrome"), shutil.which("chromium"),
    ]
    for c in candidates:
        if c and Path(c).exists():
            return c
    sys.exit("No Chrome or Edge found. Install one of them to build the PDF.")


def print_pdf(html_path: Path, pdf_path: Path) -> None:
    pdf_path.unlink(missing_ok=True)
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as profile:
        subprocess.run(
            [find_browser(), "--headless=new", "--disable-gpu", f"--user-data-dir={profile}", "--no-pdf-header-footer",
             "--generate-pdf-document-outline", f"--print-to-pdf={pdf_path}", html_path.as_uri()],
            check=False, timeout=240, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    if not pdf_path.exists() or pdf_path.stat().st_size == 0:
        sys.exit("The browser did not produce a PDF.")


def lesson_pages(pdf_path: Path) -> dict[int, int]:
    """Find the page each lesson starts on, using the 'LESSON nn' marker printed at its top."""
    text = subprocess.run(["pdftotext", "-layout", str(pdf_path), "-"], capture_output=True, text=True,
                          encoding="utf-8", errors="replace").stdout
    found: dict[int, int] = {}
    for page_no, page_text in enumerate(text.split("\f"), start=1):
        for m in re.finditer(r"LESSON (\d\d)\b", page_text):
            found.setdefault(int(m.group(1)), page_no)
    return found


def main() -> int:
    from sample_data import ensure_sample_data
    ensure_sample_data()
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    print("Running the lessons and converting them to HTML ...")
    lessons = [render_lesson(p) for p in tutor.lesson_files()]
    about = render_about()

    pages: dict[int, int] = {}
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
        html_path = Path(tmp) / "sql_tutor.html"
        for attempt in (1, 2, 3):
            html_path.write_text(assemble(lessons, about, pages), encoding="utf-8")
            print(f"Printing PDF (pass {attempt}) ...")
            print_pdf(html_path, OUT)
            if not shutil.which("pdftotext"):
                print("pdftotext not found: the contents page will have links but no page numbers.")
                break
            found = lesson_pages(OUT)
            if found == pages:
                break
            pages = found
    print(f"Done: {OUT} ({OUT.stat().st_size / 1024:,.0f} KB, {len(pages)} lesson page numbers)")
    return 0


if __name__ == "__main__":
    sys.path.insert(0, str(HERE))
    sys.exit(main())
