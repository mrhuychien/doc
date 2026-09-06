#!/usr/bin/env python3
"""
enrich.py — sinh idea_summary + 3 câu hỏi cho từng chunk, và ContextCard cho cuốn sách.

    python enrich.py books/<slug>/chunks.json            # chạy toàn bộ
    python enrich.py books/<slug>/chunks.json --dry-run   # ContextCard + chunk 1–5, in ra màn hình

Ghi ra: enriched.json + review.csv (UTF-8 có BOM để Excel mở không lỗi font).

GUARDRAIL CỨNG: trường `text` được copy nguyên từ chunks.json. enrich KHÔNG BAO GIỜ
ghi vào nó — có assert kiểm tra trước khi lưu.

Cache: ingest/.cache/<sha1(text)>.json. Cache lưu kèm hash của prompt và tên model;
sửa prompt hoặc đổi model là cache tự hỏng, không phục vụ kết quả cũ.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import os
import re
import sys
import time
from pathlib import Path

import anthropic
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent
CACHE_DIR = ROOT / ".cache"
PROMPTS = ROOT / "prompts"

MODEL = os.environ.get("ANTHROPIC_MODEL", "claude-sonnet-5")
MAX_ATTEMPTS = 3

CHUNK_SCHEMA = {
    "type": "object",
    "properties": {
        "idea_summary": {"type": "string"},
        "questions": {
            "type": "array",
            "minItems": 3,
            "maxItems": 3,
            "items": {
                "type": "object",
                "properties": {
                    "cue": {"type": "string"},
                    "expected_answer": {"type": "string"},
                },
                "required": ["cue", "expected_answer"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["idea_summary", "questions"],
    "additionalProperties": False,
}

CONTEXT_SCHEMA = {
    "type": "object",
    "properties": {"text": {"type": "string"}},
    "required": ["text"],
    "additionalProperties": False,
}

BANNED = re.compile(r"sách này|cuốn sách|đoạn trích|văn bản này|tài liệu này", re.IGNORECASE)


# ═══════════════════════════════════════════════════════════════
# Validate — cái gì prompt hứa thì ở đây kiểm
# ═══════════════════════════════════════════════════════════════

def wc(s: str) -> int:
    return len(s.split())


def longest_verbatim_run(answer: str, source: str) -> int:
    """Số từ liên tiếp dài nhất của `answer` xuất hiện y nguyên trong `source`."""
    a = answer.lower().split()
    src = " " + " ".join(source.lower().split()) + " "
    best = 0
    for i in range(len(a)):
        for j in range(i + best + 1, len(a) + 1):
            if f" {' '.join(a[i:j])} " in src:
                best = j - i
            else:
                break
    return best


def check_chunk(data: dict, text: str) -> list[str]:
    bad: list[str] = []
    idea = (data.get("idea_summary") or "").strip()
    if not idea:
        bad.append("idea_summary rỗng")
    elif wc(idea) > 25:
        bad.append(f"idea_summary {wc(idea)} từ, phải ≤ 25")
    if BANNED.search(idea):
        bad.append("idea_summary dùng 'sách này'/'cuốn sách' — phải gọi là 'tác giả'")

    qs = data.get("questions") or []
    if len(qs) != 3:
        bad.append(f"phải đúng 3 câu hỏi, đang có {len(qs)}")
    for i, q in enumerate(qs):
        cue, ans = (q.get("cue") or "").strip(), (q.get("expected_answer") or "").strip()
        if not cue or not ans:
            bad.append(f"câu {i}: thiếu cue hoặc expected_answer")
            continue
        if wc(ans) > 40:
            bad.append(f"câu {i}: expected_answer {wc(ans)} từ, phải ≤ 40")
        run = longest_verbatim_run(ans, text)
        if run > 15:
            bad.append(f"câu {i}: trích nguyên văn {run} từ liên tiếp, phải ≤ 15")
        if BANNED.search(cue) or BANNED.search(ans):
            bad.append(f"câu {i}: dùng 'sách này'/'cuốn sách' — phải gọi là 'tác giả'")
    cues = [(q.get("cue") or "").strip().lower() for q in qs]
    if len(set(cues)) < len(cues):
        bad.append("có 2 câu hỏi trùng nhau")
    return bad


def check_context(data: dict) -> list[str]:
    t = (data.get("text") or "").strip()
    bad: list[str] = []
    if not t:
        bad.append("text rỗng")
    elif wc(t) > 150:
        bad.append(f"{wc(t)} từ, phải ≤ 150")
    if '"' in t or "“" in t:
        bad.append("có dấu ngoặc kép — đây là tóm lược, không phải trích dẫn")
    return bad


# ═══════════════════════════════════════════════════════════════
# Gọi API
# ═══════════════════════════════════════════════════════════════

def sha1(s: str) -> str:
    return hashlib.sha1(s.encode("utf-8")).hexdigest()


def call(
    client: anthropic.Anthropic,
    system: str,
    user: str,
    schema: dict,
    validate,
    label: str,
) -> tuple[dict | None, list[str]]:
    """Gọi model, ép JSON theo schema, validate; hỏng thì retry có backoff."""
    problems: list[str] = []
    last: dict | None = None
    msg = user
    for attempt in range(1, MAX_ATTEMPTS + 1):
        try:
            resp = client.messages.create(
                model=MODEL,
                max_tokens=4000,
                thinking={"type": "adaptive"},
                system=[
                    {
                        "type": "text",
                        "text": system,
                        "cache_control": {"type": "ephemeral"},   # dùng lại cho mọi chunk
                    }
                ],
                messages=[{"role": "user", "content": msg}],
                output_config={
                    "effort": "medium",
                    "format": {"type": "json_schema", "schema": schema},
                },
            )
            if resp.stop_reason == "refusal":
                problems = [f"model từ chối: {getattr(resp.stop_details, 'category', '?')}"]
                break
            raw = next(b.text for b in resp.content if b.type == "text")
            data = json.loads(raw)
        except (anthropic.APIStatusError, anthropic.APIConnectionError) as exc:
            problems = [f"API lỗi: {exc}"]
            if attempt < MAX_ATTEMPTS:
                time.sleep(2 ** attempt)
            continue
        except (json.JSONDecodeError, StopIteration) as exc:
            problems = [f"JSON hỏng: {exc}"]
            msg = user + "\n\nLần trước bạn trả về không phải JSON hợp lệ. CHỈ TRẢ JSON."
            if attempt < MAX_ATTEMPTS:
                time.sleep(2 ** attempt)
            continue

        last = data
        problems = validate(data)
        if not problems:
            return data, []
        print(f"  ↻ {label} lần {attempt}: " + "; ".join(problems), file=sys.stderr)
        msg = user + "\n\nBản trước vi phạm ràng buộc sau, sửa lại:\n- " + "\n- ".join(problems)
        if attempt < MAX_ATTEMPTS:
            time.sleep(2 ** attempt)
    return last, problems


def chunk_user_message(c: dict, prev_idea: str | None) -> str:
    parts = [
        f"Chương: {c.get('chapter_title') or '—'}",
        f"Mục: {c.get('section_no') or ''} {c.get('section_title') or '—'}".strip(),
        f"Tiểu mục: {c.get('subpoint_title') or '—'}",
    ]
    if c.get("part_total", 1) > 1:
        parts.append(f"Đây là phần {c['part_index']}/{c['part_total']} của tiểu mục.")
    parts.append(
        f"Luận điểm của đoạn liền trước: {prev_idea}" if prev_idea
        else "Không có đoạn liền trước (đây là đoạn mở đầu)."
    )
    parts.append("\n--- ĐOẠN VĂN ---\n" + c["text"])
    return "\n".join(parts)


# ═══════════════════════════════════════════════════════════════
# main
# ═══════════════════════════════════════════════════════════════

def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("chunks", type=Path, help="books/<slug>/chunks.json")
    ap.add_argument("--dry-run", action="store_true",
                    help="GATE B: chỉ ContextCard + chunk seq 1–5, in ra màn hình, không ghi file")
    ap.add_argument("--limit", type=int, default=0, help="chỉ xử lý N chunk đầu (debug)")
    args = ap.parse_args()

    load_dotenv(ROOT.parent / ".env.local")
    load_dotenv(ROOT.parent / ".env")
    if not os.environ.get("ANTHROPIC_API_KEY"):
        print("❌ Thiếu ANTHROPIC_API_KEY (đặt trong .env.local)", file=sys.stderr)
        return 2

    src = args.chunks.resolve()
    book_dir = src.parent
    doc = json.loads(src.read_text(encoding="utf-8"))
    chunks: list[dict] = doc["chunks"]

    enrich_prompt = (PROMPTS / "enrich.md").read_text(encoding="utf-8")
    context_prompt = (PROMPTS / "context.md").read_text(encoding="utf-8")
    stamp = {"model": MODEL, "prompt_sha": sha1(enrich_prompt)}
    CACHE_DIR.mkdir(exist_ok=True)

    client = anthropic.Anthropic()

    # ── ContextCard (seq 0) ────────────────────────────────────
    fm_path = book_dir / "frontmatter.txt"
    if not fm_path.exists():
        print(f"❌ Thiếu {fm_path} — chạy parse_pdf.py trước", file=sys.stderr)
        return 2
    print("→ ContextCard…")
    ctx, ctx_bad = call(
        client, context_prompt,
        "Phần đầu sách:\n\n" + fm_path.read_text(encoding="utf-8"),
        CONTEXT_SCHEMA, check_context, "context",
    )
    context_card = {
        "seq": 0,
        "kind": "context",
        "chapter_no": None,
        "chapter_title": None,
        "section_no": None,
        "section_title": None,
        "subpoint_title": None,
        "part_index": 1,
        "part_total": 1,
        "text": (ctx or {}).get("text", ""),
        "word_count": wc((ctx or {}).get("text", "")),
        "page_from": None,
        "page_to": None,
        "idea_summary": None,
        "questions": None,
    }
    if ctx_bad:
        context_card["enrich_failed"] = "; ".join(ctx_bad)

    print("\n" + "═" * 60 + "\nCONTEXT CARD\n" + "═" * 60)
    print(context_card["text"])
    print(f"({context_card['word_count']} từ)" + (f"  ⚠ {ctx_bad}" if ctx_bad else ""))

    # ── chunks ─────────────────────────────────────────────────
    todo = chunks[:5] if args.dry_run else (chunks[: args.limit] if args.limit else chunks)
    out: list[dict] = []
    failed: list[tuple[int, str]] = []
    hits = 0
    prev_idea: str | None = None

    for c in todo:
        key = CACHE_DIR / f"{sha1(c['text'])}.json"
        data, bad = None, []
        if key.exists():
            cached = json.loads(key.read_text(encoding="utf-8"))
            if cached.get("model") == stamp["model"] and cached.get("prompt_sha") == stamp["prompt_sha"]:
                data, hits = cached["data"], hits + 1
        if data is None:
            print(f"→ seq {c['seq']} ({c['word_count']} từ)…")
            data, bad = call(
                client, enrich_prompt, chunk_user_message(c, prev_idea),
                CHUNK_SCHEMA, lambda d: check_chunk(d, c["text"]), f"seq {c['seq']}",
            )
            if data and not bad:
                key.write_text(
                    json.dumps({**stamp, "data": data}, ensure_ascii=False, indent=2),
                    encoding="utf-8",
                )

        rec = dict(c)
        rec["idea_summary"] = (data or {}).get("idea_summary")
        rec["questions"] = (data or {}).get("questions")
        if bad:
            rec["enrich_failed"] = "; ".join(bad)
            failed.append((c["seq"], rec["enrich_failed"]))
        assert rec["text"] == c["text"], "enrich đã sửa vào text — cấm tuyệt đối"
        out.append(rec)
        prev_idea = rec["idea_summary"] or prev_idea

    if args.dry_run:
        for r in out:
            print("\n" + "═" * 60)
            print(f"seq {r['seq']} · {r['section_no']} {r['section_title']} · "
                  f"{r['subpoint_title'] or '—'} · {r['word_count']} từ")
            print("─" * 60)
            print(f"Ý CHÍNH: {r['idea_summary']}")
            for i, q in enumerate(r["questions"] or []):
                print(f"\nQ{i} ({['nhớ lại', 'áp dụng', 'liên kết'][i]}): {q['cue']}")
                print(f"   → {q['expected_answer']}")
            if r.get("enrich_failed"):
                print(f"⚠ {r['enrich_failed']}")
        print("\n" + "═" * 60)
        print(f"DRY-RUN: ContextCard + {len(out)} chunk. Không ghi file.")
        print('GATE B — chờ chủ dự án gõ "OK enrich" để chạy toàn bộ.')
        return 0

    # ── ghi file ───────────────────────────────────────────────
    doc["chunks"] = [context_card] + out
    doc["stats"]["enriched"] = sum(1 for r in out if r.get("questions"))
    doc["stats"]["enrich_failed"] = len(failed)
    doc["stats"]["cache_hits"] = hits
    (book_dir / "enriched.json").write_text(
        json.dumps(doc, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    csv_path = book_dir / "review.csv"
    with csv_path.open("w", encoding="utf-8-sig", newline="") as f:
        w = csv.writer(f)
        w.writerow(["seq", "chapter_no", "section_no", "subpoint_title", "word_count",
                    "idea_summary", "q0", "a0", "q1", "a1", "q2", "a2"])
        for r in out:
            qs = (r.get("questions") or []) + [{}, {}, {}]
            w.writerow([
                r["seq"], r["chapter_no"], r["section_no"], r["subpoint_title"] or "",
                r["word_count"], r.get("idea_summary") or "",
                *[x for q in qs[:3] for x in (q.get("cue", ""), q.get("expected_answer", ""))],
            ])

    print(f"\n✅ enriched.json: {len(out)}/{len(chunks)} chunk "
          f"(cache hit {hits} · lỗi {len(failed)})")
    print(f"✅ review.csv: {csv_path}")
    if failed:
        print("\n⚠ enrich_failed:")
        for seq, why in failed:
            print(f"  seq {seq}: {why}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
