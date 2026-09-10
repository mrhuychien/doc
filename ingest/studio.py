#!/usr/bin/env python3
"""
studio.py — UI nạp sách, chạy LOCAL. Không deploy, không nằm trong app feed.

    ingest/.venv/bin/python ingest/studio.py      →  http://127.0.0.1:8765

Làm được: kéo thả PDF · khai báo sách (có xem trước từng trang để chọn khoảng
trang) · chạy parse/enrich/load · duyệt GATE A và GATE B bằng nút · sửa
idea_summary + 3 câu hỏi trong bảng thay cho mở Excel.

Studio chỉ GỌI parse_pdf.py / enrich.py / load.py qua subprocess — không tự
parse, không tự gọi LLM. Ba lệnh CLI trong README vẫn chạy độc lập như cũ.

BẢO MẬT: chỉ bind 127.0.0.1. Tiến trình này đọc .env.local (service role key,
Anthropic key) và chạy được lệnh trên máy bạn — đừng bao giờ expose ra ngoài.
"""

from __future__ import annotations

import csv
import json
import re
import subprocess
import sys
import threading
import time
import unicodedata
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

ROOT = Path(__file__).resolve().parent
BOOKS = ROOT / "books"
HOST, PORT = "127.0.0.1", 8765
MAX_PDF = 200 * 1024 * 1024

STEPS = ("parse", "enrich_dry", "enrich", "load")


# ═══════════════════════════════════════════════════════════════
# Job — chạy 1 script ingest, gom log để trang web poll
# ═══════════════════════════════════════════════════════════════

class Job:
    _all: dict[str, "Job"] = {}
    _lock = threading.Lock()

    def __init__(self, slug: str, step: str, argv: list[str]) -> None:
        self.id = f"{slug}-{step}-{int(time.time() * 1000)}"
        self.slug, self.step = slug, step
        self.log: list[str] = []
        self.code: int | None = None
        self.proc: subprocess.Popen | None = None
        with Job._lock:
            Job._all[self.id] = self
        threading.Thread(target=self._run, args=(argv,), daemon=True).start()

    def _run(self, argv: list[str]) -> None:
        self.log.append("$ " + " ".join(argv[1:]))
        try:
            self.proc = subprocess.Popen(
                argv,
                cwd=ROOT,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                encoding="utf-8",
                errors="replace",
                bufsize=1,
            )
            assert self.proc.stdout
            for line in self.proc.stdout:
                self.log.append(line.rstrip("\n"))
                if len(self.log) > 4000:
                    del self.log[:1000]
            self.code = self.proc.wait()
        except Exception as exc:                       # noqa: BLE001
            self.log.append(f"‼ {exc}")
            self.code = 1
        if self.code == 0:
            if self.step == "load":
                set_gate(self.slug, "loaded_at", time.strftime("%d/%m %H:%M"))
            elif self.step == "enrich_dry":
                set_gate(self.slug, "dry_ran", True)     # mở khoá nút duyệt GATE B

    def stop(self) -> None:
        if self.proc and self.code is None:
            self.proc.terminate()

    @classmethod
    def get(cls, jid: str) -> "Job | None":
        return cls._all.get(jid)


# ═══════════════════════════════════════════════════════════════
# Trạng thái 1 cuốn sách
# ═══════════════════════════════════════════════════════════════

def slugify(s: str) -> str:
    s = unicodedata.normalize("NFD", s)
    s = "".join(c for c in s if unicodedata.category(c) != "Mn")
    s = s.replace("đ", "d").replace("Đ", "D").lower()
    s = re.sub(r"[^a-z0-9]+", "-", s).strip("-")
    return s[:60] or "sach-moi"


def gates(slug: str) -> dict:
    f = BOOKS / slug / ".studio.json"
    if f.exists():
        try:
            return json.loads(f.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            pass
    return {}


def set_gate(slug: str, key: str, value) -> dict:
    g = gates(slug)
    g[key] = value
    (BOOKS / slug / ".studio.json").write_text(
        json.dumps(g, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return g


def book_state(slug: str) -> dict:
    d = BOOKS / slug
    g = gates(slug)
    cfg = {}
    if (d / "book.yaml").exists():
        try:
            import yaml

            cfg = yaml.safe_load((d / "book.yaml").read_text(encoding="utf-8")) or {}
        except Exception:                              # noqa: BLE001
            cfg = {}
    stats = {}
    if (d / "chunks.json").exists():
        try:
            stats = json.loads((d / "chunks.json").read_text(encoding="utf-8")).get("stats", {})
        except json.JSONDecodeError:
            pass
    return {
        "slug": slug,
        "title": cfg.get("title") or slug,
        "has_pdf": (d / "source.pdf").exists(),
        "pdf_mb": round((d / "source.pdf").stat().st_size / 1e6, 1)
        if (d / "source.pdf").exists()
        else 0,
        "has_yaml": (d / "book.yaml").exists(),
        "has_chunks": (d / "chunks.json").exists(),
        "has_report": (d / "parse_report.md").exists(),
        "has_enriched": (d / "enriched.json").exists(),
        "has_review": (d / "review.csv").exists(),
        "gate_a": bool(g.get("gate_a")),
        "gate_b": bool(g.get("gate_b")),
        "dry_ran": bool(g.get("dry_ran")),
        "loaded_at": g.get("loaded_at"),
        "stats": stats,
        "expect": cfg.get("expect") or {},
        "pages": cfg.get("pages") or {},
    }


def list_books() -> list[dict]:
    BOOKS.mkdir(exist_ok=True)
    return [book_state(p.name) for p in sorted(BOOKS.iterdir()) if p.is_dir()]


# ═══════════════════════════════════════════════════════════════
# Xem trước từng trang PDF — để chọn khoảng trang cho book.yaml
# ═══════════════════════════════════════════════════════════════

def page_preview(slug: str, first: int, last: int) -> list[dict]:
    import pymupdf

    path = BOOKS / slug / "source.pdf"
    out: list[dict] = []
    with pymupdf.open(path) as doc:
        last = min(last, len(doc))
        for pno in range(max(1, first), last + 1):
            raw = doc[pno - 1].get_text("text") or ""
            lines = [ln.strip() for ln in raw.split("\n") if ln.strip()]
            out.append(
                {
                    "page": pno,
                    "words": len(raw.split()),
                    "head": " / ".join(lines[:3])[:180],
                }
            )
    return out


def pdf_pages(slug: str) -> int:
    import pymupdf

    with pymupdf.open(BOOKS / slug / "source.pdf") as doc:
        return len(doc)


BOOK_YAML = """\
# Sinh bởi Ingest Studio. Sửa tay thoải mái — parse_pdf.py đọc thẳng file này.
# Số trang là SỐ TRANG IN. page_offset = (PDF page index) - (số trang in).
slug: {slug}
title: "{title}"
author_slug: {author_slug}
page_offset: {page_offset}

pages:
  frontmatter: [{fm0}, {fm1}]   # lời tựa / lời dịch giả → frontmatter.txt (ContextCard)
  toc: [{toc0}, {toc1}]         # mục lục — ground truth để đối chiếu
  body: [{body0}, {body1}]      # thân sách — nguồn duy nhất của chunk

expect:
  chapters: {chapters}
  sections_per_chapter: {sections}
  sections_auto_min: {auto_min}
  chunks: [{c0}, {c1}]

chunk:
  target_min: 250
  target_max: 350
  split_above: 450
  min_part: 150
  merge_below: 120
{author_block}"""

AUTHOR_BLOCK = """
# author/book chỉ cần khi slug chưa có trong DB — load.py dùng để tạo hàng mới.
author:
  name: "{name}"
  name_native: "{name_native}"
  born_year: {born_year}
  monogram: "{monogram}"
  bio_short: ""
book:
  license: {license}
  year_original: {year_original}
  translator: "{translator}"
  pace_per_day: {pace}
"""


def write_book_yaml(body: dict) -> str:
    slug = slugify(body.get("slug") or body.get("title") or "")
    d = BOOKS / slug
    d.mkdir(parents=True, exist_ok=True)
    secs = body.get("sections_per_chapter") or []
    author = ""
    if body.get("author_name"):
        author = AUTHOR_BLOCK.format(
            name=body["author_name"],
            name_native=body.get("author_native", ""),
            born_year=body.get("born_year") or "null",
            monogram=body.get("monogram") or "".join(
                w[0] for w in str(body["author_name"]).split()[:2]
            ).upper(),
            license=body.get("license") or "personal",
            year_original=body.get("year_original") or "null",
            translator=body.get("translator", ""),
            pace=body.get("pace_per_day") or 5,
        )
    (d / "book.yaml").write_text(
        BOOK_YAML.format(
            slug=slug,
            title=(body.get("title") or slug).replace('"', "'"),
            author_slug=slugify(body.get("author_slug") or body.get("author_name") or "khuyet-danh"),
            page_offset=int(body.get("page_offset") or 0),
            fm0=body["frontmatter"][0], fm1=body["frontmatter"][1],
            toc0=body["toc"][0], toc1=body["toc"][1],
            body0=body["body"][0], body1=body["body"][1],
            chapters=int(body.get("chapters") or len(secs) or 0),
            sections=json.dumps(secs),
            auto_min=max(0, sum(secs) - 2) if secs else 0,
            c0=body.get("chunks_min") or 0,
            c1=body.get("chunks_max") or 99999,
            author_block=author,
        ),
        encoding="utf-8",
    )
    if not (d / "overrides.yaml").exists():
        (d / "overrides.yaml").write_text(
            "# Ghim tay điểm bắt đầu của mục không có dòng heading trong thân sách.\n"
            "# - section: \"6.5\"\n#   page: 165\n#   starts_with: \"...\"\n\nsections: []\n",
            encoding="utf-8",
        )
    return slug


# ═══════════════════════════════════════════════════════════════
# review.csv ↔ bảng trên web
# ═══════════════════════════════════════════════════════════════

REVIEW_COLS = ["seq", "chapter_no", "section_no", "subpoint_title", "word_count",
               "idea_summary", "q0", "a0", "q1", "a1", "q2", "a2"]


def read_review(slug: str) -> list[dict]:
    f = BOOKS / slug / "review.csv"
    if not f.exists():
        return []
    with f.open(encoding="utf-8-sig", newline="") as fh:
        return list(csv.DictReader(fh))


def write_review(slug: str, rows: list[dict]) -> int:
    f = BOOKS / slug / "review.csv"
    with f.open("w", encoding="utf-8-sig", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=REVIEW_COLS, extrasaction="ignore")
        w.writeheader()
        for r in rows:
            w.writerow({c: r.get(c, "") for c in REVIEW_COLS})
    return len(rows)


def chunk_text(slug: str, seq: int) -> str:
    """Văn bản gốc của 1 chunk — để đối chiếu khi sửa câu hỏi."""
    for name in ("enriched.json", "chunks.json"):
        f = BOOKS / slug / name
        if not f.exists():
            continue
        doc = json.loads(f.read_text(encoding="utf-8"))
        for c in doc.get("chunks", []):
            if int(c.get("seq", -1)) == seq:
                return c.get("text", "")
    return ""


# ═══════════════════════════════════════════════════════════════
# HTTP
# ═══════════════════════════════════════════════════════════════

class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "IngestStudio"

    def log_message(self, fmt: str, *args) -> None:      # bớt ồn
        if "/api/jobs/" not in (args[0] if args else ""):
            sys.stderr.write("  %s\n" % (fmt % args))

    # ── helpers ──
    def _send(self, code: int, body: bytes, ctype: str) -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _json(self, data, code: int = 200) -> None:
        self._send(code, json.dumps(data, ensure_ascii=False).encode(), "application/json; charset=utf-8")

    def _err(self, msg: str, code: int = 400) -> None:
        self._json({"error": msg}, code)

    def _body(self) -> bytes:
        n = int(self.headers.get("Content-Length") or 0)
        if n > MAX_PDF:
            raise ValueError("file quá lớn")
        return self.rfile.read(n)

    def _safe_slug(self, raw: str) -> str | None:
        s = slugify(raw)
        return s if s and (BOOKS / s).resolve().parent == BOOKS.resolve() else None

    # ── GET ──
    def do_GET(self) -> None:                            # noqa: N802
        u = urlparse(self.path)
        q = parse_qs(u.query)
        p = u.path

        if p in ("/", "/index.html"):
            html = (ROOT / "studio.html").read_bytes()
            return self._send(200, html, "text/html; charset=utf-8")

        if p == "/api/books":
            return self._json({"books": list_books()})

        m = re.fullmatch(r"/api/books/([^/]+)/(pages|file|review|chunk)", p)
        if m:
            slug = self._safe_slug(m.group(1))
            if not slug:
                return self._err("slug không hợp lệ")
            what = m.group(2)
            d = BOOKS / slug
            try:
                if what == "pages":
                    if not (d / "source.pdf").exists():
                        return self._err("chưa có source.pdf")
                    first = int(q.get("from", ["1"])[0])
                    return self._json({
                        "total": pdf_pages(slug),
                        "pages": page_preview(slug, first, first + int(q.get("n", ["40"])[0]) - 1),
                    })
                if what == "file":
                    name = q.get("name", [""])[0]
                    if name not in ("parse_report.md", "overrides.yaml", "book.yaml", "frontmatter.txt"):
                        return self._err("file không cho đọc")
                    f = d / name
                    return self._json({"name": name, "text": f.read_text(encoding="utf-8") if f.exists() else ""})
                if what == "review":
                    return self._json({"rows": read_review(slug)})
                if what == "chunk":
                    return self._json({"text": chunk_text(slug, int(q.get("seq", ["0"])[0]))})
            except Exception as exc:                     # noqa: BLE001
                return self._err(str(exc), 500)

        m = re.fullmatch(r"/api/jobs/([\w.-]+)", p)
        if m:
            job = Job.get(m.group(1))
            if not job:
                return self._err("job không tồn tại", 404)
            since = int(q.get("since", ["0"])[0])
            return self._json({
                "id": job.id, "step": job.step, "code": job.code,
                "done": job.code is not None,
                "from": since, "log": job.log[since:], "total": len(job.log),
                "book": book_state(job.slug),
            })

        self._err("không có route này", 404)

    # ── POST / PUT ──
    def do_PUT(self) -> None:                            # noqa: N802
        self.do_POST()

    def do_POST(self) -> None:                           # noqa: N802
        u = urlparse(self.path)
        q = parse_qs(u.query)
        p = u.path

        try:
            if p == "/api/books":
                body = json.loads(self._body() or b"{}")
                for k in ("frontmatter", "toc", "body"):
                    if not (isinstance(body.get(k), list) and len(body[k]) == 2):
                        return self._err(f"thiếu khoảng trang: {k}")
                slug = write_book_yaml(body)
                return self._json({"slug": slug, "book": book_state(slug)})

            m = re.fullmatch(r"/api/books/([^/]+)/(pdf|run|gate|file|review)", p)
            if not m:
                return self._err("không có route này", 404)
            slug = self._safe_slug(m.group(1))
            if not slug:
                return self._err("slug không hợp lệ")
            what = m.group(2)
            d = BOOKS / slug

            if what == "pdf":
                data = self._body()
                if data[:5] != b"%PDF-":
                    return self._err("không phải file PDF")
                d.mkdir(parents=True, exist_ok=True)    # chỉ tạo thư mục khi file hợp lệ
                (d / "source.pdf").write_bytes(data)
                return self._json({"book": book_state(slug), "total_pages": pdf_pages(slug)})

            if what in ("file", "review", "gate", "run") and not d.is_dir():
                return self._err("chưa có sách này", 404)

            if what == "file":
                body = json.loads(self._body() or b"{}")
                name = body.get("name")
                if name not in ("overrides.yaml", "book.yaml"):
                    return self._err("file không cho ghi")
                (d / name).write_text(body.get("text", ""), encoding="utf-8")
                return self._json({"ok": True})

            if what == "review":
                body = json.loads(self._body() or b"{}")
                return self._json({"saved": write_review(slug, body.get("rows") or [])})

            if what == "gate":
                body = json.loads(self._body() or b"{}")
                key, val = body.get("key"), bool(body.get("value"))
                if key not in ("gate_a", "gate_b"):
                    return self._err("gate không hợp lệ")
                if key == "gate_a" and val and not (d / "chunks.json").exists():
                    return self._err("chưa có chunks.json — chạy parse trước")
                if key == "gate_b" and val and not gates(slug).get("dry_ran"):
                    return self._err("phải chạy thử 5 chunk trước khi duyệt GATE B")
                set_gate(slug, key, val)
                return self._json({"book": book_state(slug)})

            if what == "run":
                body = json.loads(self._body() or b"{}")
                step = body.get("step")
                if step not in STEPS:
                    return self._err("step không hợp lệ")
                g = gates(slug)
                if step in ("enrich_dry", "enrich") and not g.get("gate_a"):
                    return self._err("GATE A chưa duyệt — đọc parse_report rồi bấm OK parse")
                if step == "enrich" and not g.get("gate_b"):
                    return self._err("GATE B chưa duyệt — chạy thử 5 chunk rồi bấm OK enrich")
                if step == "load" and not (d / "enriched.json").exists():
                    return self._err("chưa có enriched.json")

                py = sys.executable
                argv = {
                    "parse": [py, str(ROOT / "parse_pdf.py"), f"books/{slug}/source.pdf"],
                    "enrich_dry": [py, str(ROOT / "enrich.py"), f"books/{slug}/chunks.json", "--dry-run"],
                    "enrich": [py, str(ROOT / "enrich.py"), f"books/{slug}/chunks.json"],
                    "load": [py, str(ROOT / "load.py"), f"books/{slug}/enriched.json"],
                }[step]
                if step == "parse":                      # chunks đổi → mọi thứ sau đó phải duyệt lại
                    for k in ("gate_a", "gate_b", "dry_ran"):
                        set_gate(slug, k, False)
                return self._json({"job": Job(slug, step, argv).id})
        except Exception as exc:                         # noqa: BLE001
            return self._err(str(exc), 500)


def main() -> int:
    BOOKS.mkdir(exist_ok=True)
    if not (ROOT / "studio.html").exists():
        print("❌ Thiếu ingest/studio.html", file=sys.stderr)
        return 2
    srv = ThreadingHTTPServer((HOST, PORT), Handler)
    print(f"\n  Ingest Studio  →  http://{HOST}:{PORT}\n"
          f"  Thư mục sách   →  {BOOKS}\n"
          f"  Ctrl+C để dừng.\n")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\nĐã dừng.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
