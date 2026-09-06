#!/usr/bin/env python3
"""
load.py — nạp enriched.json vào Supabase. Chạy bao nhiêu lần cũng ra một kết quả.

    python load.py books/<slug>/enriched.json
    python load.py books/<slug>/enriched.json --dry-run

Idempotent theo (book_id, seq) — upsert, không insert mù.
Ưu tiên giá trị đã sửa tay trong review.csv (ô nào để trống thì giữ giá trị của enriched.json).
Cập nhật book.total_chunks = số chunk kind='excerpt'.

Chạy bằng SERVICE ROLE KEY nên bypass RLS. Key này chỉ để local, không bao giờ lên Vercel.
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import sys
from pathlib import Path

import yaml
from dotenv import load_dotenv
from supabase import create_client

ROOT = Path(__file__).resolve().parent
BATCH = 200
CHUNK_COLS = [
    "book_id", "seq", "kind", "chapter_no", "chapter_title", "section_no",
    "section_title", "subpoint_title", "part_index", "part_total", "text",
    "word_count", "page_from", "page_to", "idea_summary", "questions",
]


def read_overrides(path: Path) -> dict[int, dict]:
    """review.csv → {seq: {field: giá trị người dùng đã sửa}}. Ô trống = không đổi."""
    if not path.exists():
        return {}
    out: dict[int, dict] = {}
    with path.open(encoding="utf-8-sig", newline="") as f:
        for row in csv.DictReader(f):
            try:
                seq = int(row["seq"])
            except (KeyError, TypeError, ValueError):
                continue
            patch: dict = {}
            if (row.get("idea_summary") or "").strip():
                patch["idea_summary"] = row["idea_summary"].strip()
            qs = []
            for i in range(3):
                cue = (row.get(f"q{i}") or "").strip()
                ans = (row.get(f"a{i}") or "").strip()
                if cue and ans:
                    qs.append({"cue": cue, "expected_answer": ans})
            if len(qs) == 3:
                patch["questions"] = qs
            if patch:
                out[seq] = patch
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("enriched", type=Path, help="books/<slug>/enriched.json")
    ap.add_argument("--dry-run", action="store_true", help="chỉ in ra sẽ ghi gì, không gọi Supabase")
    args = ap.parse_args()

    load_dotenv(ROOT.parent / ".env.local")
    load_dotenv(ROOT.parent / ".env")
    url = os.environ.get("SUPABASE_URL")
    key = os.environ.get("SUPABASE_SERVICE_ROLE_KEY")

    src = args.enriched.resolve()
    book_dir = src.parent
    doc = json.loads(src.read_text(encoding="utf-8"))
    meta = doc["book"]
    chunks: list[dict] = doc["chunks"]

    cfg_path = book_dir / "book.yaml"
    cfg = yaml.safe_load(cfg_path.read_text(encoding="utf-8")) if cfg_path.exists() else {}

    # ── áp review.csv đè lên enriched.json ─────────────────────
    patches = read_overrides(book_dir / "review.csv")
    n_patched = 0
    for c in chunks:
        p = patches.get(c["seq"])
        if not p:
            continue
        if any(c.get(k) != v for k, v in p.items()):
            n_patched += 1
        c.update(p)
    print(f"review.csv: {len(patches)} dòng, {n_patched} chunk lấy giá trị sửa tay")

    excerpts = [c for c in chunks if c["kind"] == "excerpt"]
    total = len(excerpts)
    missing = [c["seq"] for c in excerpts if not c.get("questions")]
    print(f"chunk: {len(chunks)} (excerpt {total} · context {len(chunks) - total})")
    if missing:
        print(f"⚠ {len(missing)} chunk chưa có câu hỏi: {missing[:20]}"
              f"{'…' if len(missing) > 20 else ''}")

    if args.dry_run:
        print("\n--dry-run: dừng ở đây, không ghi Supabase.")
        return 0
    if not url or not key:
        print("❌ Thiếu SUPABASE_URL / SUPABASE_SERVICE_ROLE_KEY (đặt trong .env.local)",
              file=sys.stderr)
        return 2

    db = create_client(url, key)

    # ── author ─────────────────────────────────────────────────
    author_slug = meta.get("author_slug") or (cfg.get("author") or {}).get("slug")
    if not author_slug:
        print("❌ book.yaml thiếu author_slug", file=sys.stderr)
        return 2
    got = db.table("author").select("id").eq("slug", author_slug).execute().data
    if got:
        author_id = got[0]["id"]
    else:
        seed = cfg.get("author")
        if not seed:
            print(f"❌ Chưa có author '{author_slug}' trong DB và book.yaml không khai báo "
                  f"khối `author:` để tạo mới.", file=sys.stderr)
            return 2
        author_id = (
            db.table("author").insert({**seed, "slug": author_slug})
            .execute().data[0]["id"]
        )
        print(f"+ author {author_slug}")

    # ── book ───────────────────────────────────────────────────
    book_slug = meta["slug"]
    fields = {
        "author_id": author_id,
        "title": meta["title"],
        "source_file": meta.get("source_file"),
        "total_chunks": total,
        **{k: v for k, v in (cfg.get("book") or {}).items()},
    }
    got = db.table("book").select("id").eq("slug", book_slug).execute().data
    if got:
        book_id = got[0]["id"]
        db.table("book").update(fields).eq("id", book_id).execute()
    else:
        fields.setdefault("license", "personal")
        book_id = db.table("book").insert({**fields, "slug": book_slug}).execute().data[0]["id"]
        print(f"+ book {book_slug}")

    # ── chunks: upsert theo (book_id, seq) ─────────────────────
    rows = [
        {**{k: c.get(k) for k in CHUNK_COLS if k != "book_id"}, "book_id": book_id}
        for c in chunks
    ]
    for i in range(0, len(rows), BATCH):
        db.table("chunk").upsert(rows[i:i + BATCH], on_conflict="book_id,seq").execute()
        print(f"  upsert {min(i + BATCH, len(rows))}/{len(rows)}")

    stale = (
        db.table("chunk").select("seq").eq("book_id", book_id)
        .gt("seq", total).execute().data
    )
    if stale:
        print(f"⚠ DB còn {len(stale)} chunk seq > {total} từ lần nạp trước "
              f"(bản parse cũ dài hơn): {[s['seq'] for s in stale][:10]}…\n"
              f"  Xoá tay nếu chắc chắn: delete from chunk "
              f"where book_id = {book_id} and seq > {total};")

    print(f"\n✅ book '{book_slug}' (id {book_id}) · total_chunks = {total} · "
          f"{len(rows)} chunk đã upsert")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
