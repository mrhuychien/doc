#!/usr/bin/env python3
"""
parse_pdf.py — cắt 1 cuốn PDF (có text layer) thành chunks.json. Deterministic, KHÔNG dùng LLM.

    python parse_pdf.py books/<slug>/source.pdf

Đọc cấu hình ở books/<slug>/book.yaml, ghi ra cùng thư mục:
    chunks.json · parse_report.md · frontmatter.txt

Nguyên tắc bất di bất dịch: chunk.text là văn bản sách NGUYÊN VĂN.
Chỉ được normalize Unicode/khoảng trắng — không sửa, không tóm tắt, không "làm sạch".

Vì sao dùng hai thư viện (xem README §4):
  · PyMuPDF  → ranh giới đoạn (get_text("blocks")), nhưng RỚT khoảng trắng tiếng Việt.
  · pdfplumber → tách chữ đúng, nhưng mất ranh giới đoạn.
Lấy bbox từ PyMuPDF, lấy chữ từ pdfplumber crop theo bbox đó.
"""

from __future__ import annotations

import argparse
import difflib
import json
import math
import re
import statistics
import sys
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path

import pymupdf as fitz  # PyMuPDF
import pdfplumber
import yaml

# ═══════════════════════════════════════════════════════════════
# Chuẩn hoá text
# ═══════════════════════════════════════════════════════════════

_WS = re.compile(r"[ \t   ]+")


def normalize_text(s: str) -> str:
    """NFC + NBSP→space + gộp khoảng trắng. Nối dòng trong đoạn bằng 1 space
    (tiếng Việt không ngắt từ bằng gạch nối)."""
    if not s:
        return ""
    s = unicodedata.normalize("NFC", s)
    s = s.replace("­", "")          # soft hyphen
    s = s.replace("\f", "\n")
    s = "\n".join(_WS.sub(" ", ln).strip() for ln in s.split("\n"))
    s = re.sub(r"\n+", " ", s)           # 1 block = 1 đoạn → nối dòng bằng space
    return _WS.sub(" ", s).strip()


_TITLE_DROP = re.compile(r"[“”\"'’‘,.;:]")


def norm_title(s: str) -> str:
    """Dạng chuẩn để so khớp TOC ↔ thân sách."""
    s = unicodedata.normalize("NFC", s or "")
    s = _TITLE_DROP.sub("", s)
    s = _WS.sub(" ", s.replace("\n", " ")).strip()
    return s.casefold()


def wc(s: str) -> int:
    return len(s.split())


_ROMAN = {"I": 1, "V": 5, "X": 10}


def roman_to_int(r: str) -> int | None:
    r = r.strip().upper()
    if not r or any(c not in _ROMAN for c in r):
        return None
    total, prev = 0, 0
    for c in reversed(r):
        v = _ROMAN[c]
        total = total - v if v < prev else total + v
        prev = max(prev, v)
    return total


# ═══════════════════════════════════════════════════════════════
# Block — 1 đoạn văn trên 1 trang
# ═══════════════════════════════════════════════════════════════

@dataclass
class Block:
    page: int
    bbox: tuple[float, float, float, float]
    text: str                 # chữ lấy từ pdfplumber (nguồn sự thật)
    mu_text: str              # chữ của PyMuPDF, chỉ để đối chiếu word_count
    source: str = "pdfplumber"
    warn: str | None = None

    @property
    def words(self) -> int:
        return wc(self.text)

    @property
    def nlines(self) -> int:
        return len([l for l in self.mu_text.split("\n") if l.strip()])


_PAGENO = re.compile(r"^\d{1,3}$")
_SEP = re.compile(r"^[-–—_.·•\s]+$")


def is_noise(b: Block, page_height: float) -> bool:
    """Số trang (block số đứng riêng ở đáy trang) và dấu `-` dưới heading."""
    t = b.text.strip()
    if not t:
        return True
    if _SEP.match(t):
        return True
    if _PAGENO.match(t) and b.bbox[1] > page_height * 0.85:
        return True
    return False


def extract_page_blocks(
    fpage, ppage, pno: int, pad: float, warnings: list[str]
) -> list[Block]:
    """bbox từ PyMuPDF → crop pdfplumber → text đúng dấu cách."""
    ox, oy = ppage.bbox[0], ppage.bbox[1]
    px1, py1 = ppage.bbox[2], ppage.bbox[3]

    if abs((px1 - ox) - fpage.rect.width) > 1.5 or abs((py1 - oy) - fpage.rect.height) > 1.5:
        warnings.append(
            f"tr.{pno}: kích thước trang lệch giữa 2 thư viện "
            f"(fitz {fpage.rect.width:.0f}x{fpage.rect.height:.0f} vs "
            f"pdfplumber {px1 - ox:.0f}x{py1 - oy:.0f})"
        )

    out: list[Block] = []
    for x0, y0, x1, y1, mu_raw, _no, btype in fpage.get_text("blocks", sort=True):
        if btype != 0:                       # 0 = text block
            continue
        mu_text = normalize_text(mu_raw)
        if not mu_text:
            continue

        text, used_pad = "", pad
        for trial in (pad, pad + 1.0):       # nới 1pt, nếu lệch thì thử 2pt
            crop = (
                max(ox, ox + x0 - trial),
                max(oy, oy + y0 - trial),
                min(px1, ox + x1 + trial),
                min(py1, oy + y1 + trial),
            )
            if crop[2] - crop[0] <= 0 or crop[3] - crop[1] <= 0:
                continue
            try:
                got = ppage.crop(crop).extract_text() or ""
            except Exception as exc:         # crop ngoài mediabox
                warnings.append(f"tr.{pno}: pdfplumber crop lỗi ({exc})")
                got = ""
            got = normalize_text(got)
            text, used_pad = got, trial
            if got and abs(wc(got) - wc(mu_text)) <= max(1, 0.10 * wc(mu_text)):
                break

        warn = None
        source = "pdfplumber"
        if not text:
            text, source = mu_text, "pymupdf"
            warn = "pdfplumber crop rỗng → dùng text PyMuPDF (có thể rớt dấu cách)"
        elif abs(wc(text) - wc(mu_text)) > max(1, 0.10 * wc(mu_text)):
            warn = (
                f"word_count lệch >10% sau khi nới {used_pad:.0f}pt "
                f"(pdfplumber {wc(text)} vs PyMuPDF {wc(mu_text)})"
            )
        if warn:
            warnings.append(f"tr.{pno}: {warn} — «{text[:60]}…»")

        out.append(Block(pno, (x0, y0, x1, y1), text, mu_text, source, warn))
    return out


# ═══════════════════════════════════════════════════════════════
# Mục lục = ground truth
# ═══════════════════════════════════════════════════════════════

RE_TOC_CHAPTER = re.compile(r"^([IVX]+)/\s*(.+?)\s*\(\s*tr\.\s*(\d+)\s*\)\s*$")
RE_TOC_SECTION_LEADER = re.compile(r"^(.+?)\s*[.…]{2,}\s*\.?\s*\(\s*tr\.\s*(\d+)\s*\)\s*$")
RE_TOC_SECTION_BARE = re.compile(r"^(.+?)\s*\(\s*tr\.\s*(\d+)\s*\)\s*$")
RE_ORDINAL_ONLY = re.compile(r"^\d{1,2}\s*[.:]?\s*$")


@dataclass
class TocSection:
    no: str
    title: str
    page: int


@dataclass
class TocChapter:
    no: int
    roman: str
    title: str
    page: int
    sections: list[TocSection] = field(default_factory=list)


def parse_toc(pdf, first: int, last: int) -> list[TocChapter]:
    lines: list[str] = []
    for pno in range(first, last + 1):
        page = pdf.pages[pno - 1]
        raw = page.extract_text() or ""
        page.flush_cache()
        for ln in raw.split("\n"):
            ln = normalize_text(ln)
            if ln:
                lines.append(ln)

    chapters: list[TocChapter] = []
    pending = ""                     # nửa đầu của 1 entry bị wrap xuống dòng
    for ln in lines:
        if RE_ORDINAL_ONLY.match(ln):   # "1." đứng riêng trước cụm tiêu đề (và số trang)
            continue

        # Thử cả bản ghép-với-dòng-trước lẫn bản trần. Ghép trước cho entry bị wrap
        # ("Trước khi giành chính" + "quyền …(tr.45)"), trần sau để 1 dòng rác đứng
        # trên ("MỤC LỤC") không nuốt mất entry.
        cands = [normalize_text(f"{pending} {ln}"), ln] if pending else [ln]

        hit = False
        for cand in cands:                                  # chương ưu tiên tuyệt đối
            m = RE_TOC_CHAPTER.match(cand)
            if m and roman_to_int(m.group(1)):
                chapters.append(
                    TocChapter(
                        roman_to_int(m.group(1)), m.group(1),
                        normalize_text(m.group(2)), int(m.group(3)),
                    )
                )
                pending, hit = "", True
                break
        if hit:
            continue

        for cand in cands:
            m = RE_TOC_SECTION_LEADER.match(cand) or RE_TOC_SECTION_BARE.match(cand)
            if not (m and chapters):
                continue
            title = normalize_text(re.sub(r"^\d{1,2}\s*[.:]\s*", "", m.group(1)))
            title = normalize_text(title.rstrip(". …"))
            if not title:
                continue
            ch = chapters[-1]
            ch.sections.append(
                TocSection(f"{ch.no}.{len(ch.sections) + 1}", title, int(m.group(2)))
            )
            pending, hit = "", True
            break
        if hit:
            continue

        pending = ln if "(tr." not in ln and len(ln) < 120 else ""
    return chapters


# ═══════════════════════════════════════════════════════════════
# Heading trong thân sách — regex khoan dung nhưng có neo
# ═══════════════════════════════════════════════════════════════

RE_BODY_CHAPTER = re.compile(r"^Chương\s+([IVX]+)\s*[:.]\s*(.+)$", re.IGNORECASE)
RE_BODY_SECTION = re.compile(r"^(\d)\s*[.:]\s*(\d{1,2})\s*[.:]?\s+(\D.+)$")
RE_BODY_SUBPOINT = re.compile(r"^(\d{1,2})\.\s+(\S.{2,80})$")
# Chặn "5.000 đồng công quỹ", "2.800 NDT/tấn" — nhưng KHÔNG chặn tiêu đề thật
# chứa chữ "đồng" thường ("đồng thời", "cộng đồng"): phải là SỐ + đơn vị.
RE_SECTION_VETO = re.compile(r"\d[\d.,]*\s*(?:đồng|NDT|tấn|%)", re.IGNORECASE)


def as_chapter(b: Block) -> tuple[int, str] | None:
    m = RE_BODY_CHAPTER.match(b.text.strip())
    if not m:
        return None
    n = roman_to_int(m.group(1))
    return (n, normalize_text(m.group(2))) if n else None


def as_section(b: Block) -> tuple[str, str] | None:
    t = b.text.strip()
    m = RE_BODY_SECTION.match(t)
    if not m:
        return None
    title = normalize_text(m.group(3))
    # "5.000 đồng công quỹ", "2.800 NDT/tấn" — không phải heading
    if RE_SECTION_VETO.search(t):
        return None
    if b.words > 25:
        return None
    return f"{int(m.group(1))}.{int(m.group(2))}", title


def as_subpoint(b: Block) -> str | None:
    if b.nlines != 1:
        return None
    t = b.text.strip()
    m = RE_BODY_SUBPOINT.match(t)
    if not m:
        return None
    title = normalize_text(m.group(2))
    if not title or title.endswith(".") or wc(title) > 14:
        return None
    return title


# ═══════════════════════════════════════════════════════════════
# Cây: Chương → Mục → Tiểu mục → đoạn
# ═══════════════════════════════════════════════════════════════

@dataclass
class Subpoint:
    title: str
    blocks: list[Block] = field(default_factory=list)


@dataclass
class Section:
    no: str
    title: str
    page: int
    resolved_by: str = "auto"        # auto | page_fallback | override
    lead: list[Block] = field(default_factory=list)
    subpoints: list[Subpoint] = field(default_factory=list)

    def add(self, b: Block) -> None:
        (self.subpoints[-1].blocks if self.subpoints else self.lead).append(b)


@dataclass
class Chapter:
    no: int
    title: str
    sections: list[Section] = field(default_factory=list)


@dataclass
class Mark:
    """Một mốc cấu trúc gắn vào block thứ `idx` của thân sách."""
    idx: int
    kind: str          # chapter | section | subpoint
    no: str
    title: str
    how: str = "auto"  # auto | page_fallback | override


_MARK_ORDER = {"chapter": 0, "section": 1, "subpoint": 2}


def scan_marks(blocks: list[Block], toc: list[TocChapter]) -> tuple[list[Mark], list[str]]:
    """Pass 1 — chỉ nhận heading do regex bắt được. Không đoán, không fallback."""
    log: list[str] = []
    by_no = {c.no: c for c in toc}
    toc_sections = {s.no: s for c in toc for s in c.sections}

    marks: list[Mark] = []
    cur_ch: int | None = None
    seen: set[str] = set()

    for i, b in enumerate(blocks):
        hit_ch = as_chapter(b)
        if hit_ch:
            no, title = hit_ch
            tc = by_no.get(no)
            ratio = (
                difflib.SequenceMatcher(None, norm_title(title), norm_title(tc.title)).ratio()
                if tc else 0.0
            )
            log.append(
                f"Chương {no} tr.{b.page}: «{title}»"
                + (f" ↔ TOC «{tc.title}» ratio={ratio:.2f}" if tc else " ⚠ không có trong mục lục")
                + ("" if ratio >= 0.85 or not tc else " ⚠ tiêu đề lệch")
            )
            marks.append(Mark(i, "chapter", str(no), tc.title if tc and ratio >= 0.85 else title))
            cur_ch = no
            continue

        hit_sec = as_section(b)
        if hit_sec and cur_ch is not None:
            no, title = hit_sec
            ts = toc_sections.get(no)
            if ts is None:
                log.append(f"  ⚠ {no} tr.{b.page} «{title}» — không có trong mục lục, coi là văn bản")
            elif no in seen:
                log.append(f"  ⚠ {no} tr.{b.page} — trùng mốc đã có, coi là văn bản")
            else:
                ratio = difflib.SequenceMatcher(
                    None, norm_title(title), norm_title(ts.title)
                ).ratio()
                seen.add(no)
                marks.append(Mark(i, "section", no, ts.title))
                log.append(
                    f"  {no} · auto tr.{b.page} ratio={ratio:.2f}"
                    + ("" if ratio >= 0.85 else f" ⚠ lệch: body «{title}» vs TOC «{ts.title}»")
                )
                continue

        # Nhận tiểu mục ở mọi chỗ trong chương: nếu mục mở bằng page_fallback
        # (pass 2) thì lúc quét pass 1 chưa có mốc mục nào để dựa vào.
        hit_sp = as_subpoint(b)
        if hit_sp and cur_ch is not None:
            marks.append(Mark(i, "subpoint", "", hit_sp))
    return marks, log


def resolve_missing(
    blocks: list[Block],
    toc: list[TocChapter],
    marks: list[Mark],
    overrides: dict,
    log: list[str],
    warnings: list[str],
) -> list[Mark]:
    """Pass 2 — mục có trong mục lục nhưng KHÔNG có dòng heading trong thân sách.
    Ưu tiên `overrides.yaml`; không có thì bắt đầu tại block đầu tiên của trang in đó.
    Chỉ chấp nhận vị trí nằm đúng giữa mục trước và mục sau — sai cửa sổ thì báo, không nhét bừa."""
    order = [s.no for c in toc for s in c.sections]
    ts_by_no = {s.no: s for c in toc for s in c.sections}
    sec_idx = {m.no: m.idx for m in marks if m.kind == "section"}
    ch_idx = {m.no: m.idx for m in marks if m.kind == "chapter"}
    pinned = {
        str(o["section"]): o for o in (overrides.get("sections") or []) if o.get("section")
    }

    added: list[Mark] = []
    for k, no in enumerate(order):
        if no in sec_idx:
            continue
        ts = ts_by_no[no]

        lo = ch_idx.get(no.split(".")[0], -1)
        for prev in reversed(order[:k]):
            if prev in sec_idx:
                lo = max(lo, sec_idx[prev])
                break
        hi = len(blocks)
        for nxt in order[k + 1:]:
            if nxt in sec_idx:
                hi = sec_idx[nxt]
                break

        cand, how = None, None
        o = pinned.get(no)
        if o:
            page = int(o.get("page", ts.page))
            sw = norm_title(normalize_text(str(o.get("starts_with") or "")))
            for i in range(lo + 1, hi):
                b = blocks[i]
                if b.page != page:
                    continue
                if sw and not norm_title(b.text).startswith(sw):
                    continue
                cand, how = i, "override"
                break
            if cand is None:
                warnings.append(f"{no}: overrides.yaml ghim tr.{page} nhưng không thấy block khớp")
        if cand is None:
            for i in range(lo + 1, hi):
                if blocks[i].page == ts.page:
                    cand, how = i, "page_fallback"
                    break

        if cand is None:
            log.append(f"  ❌ {no} «{ts.title}» (tr.{ts.page}) — KHÔNG resolve được")
            continue
        added.append(Mark(cand, "section", no, ts.title, how))
        sec_idx[no] = cand
        log.append(f"  {no} · {how} tr.{blocks[cand].page} (không có dòng heading trong thân sách)")
    return added


def build_tree(
    blocks: list[Block], toc: list[TocChapter], overrides: dict, warnings: list[str]
) -> tuple[list[Chapter], list[str]]:
    marks, log = scan_marks(blocks, toc)
    marks = marks + resolve_missing(blocks, toc, marks, overrides, log, warnings)
    marks.sort(key=lambda m: (m.idx, _MARK_ORDER[m.kind]))

    at: dict[int, list[Mark]] = {}
    for m in marks:
        at.setdefault(m.idx, []).append(m)

    chapters: list[Chapter] = []
    cur_ch: Chapter | None = None
    cur_sec: Section | None = None
    intro: list[Block] = []      # đoạn nằm giữa heading chương và mục đầu tiên

    def flush_intro() -> None:
        """Chương có mục → dồn đoạn dẫn nhập vào lead của mục đầu (không đẻ chunk mồ côi).
        Chương không có mục nào → giữ lại thành mục giả <ch>.0 để không mất chữ."""
        nonlocal intro
        if not intro:
            return
        if cur_ch is not None:
            sec = Section(f"{cur_ch.no}.0", "", intro[0].page, "auto")
            sec.lead = intro
            cur_ch.sections.append(sec)
        intro = []

    for i, b in enumerate(blocks):
        eaten = False          # block này là dòng heading → không đưa vào text
        for m in at.get(i, []):
            if m.kind == "chapter":
                flush_intro()
                cur_ch = Chapter(int(m.no), m.title)
                chapters.append(cur_ch)
                cur_sec = None
                eaten = True
            elif m.kind == "section":
                if cur_ch is None:
                    warnings.append(f"tr.{b.page}: mục {m.no} nằm ngoài mọi chương — bỏ qua")
                    continue
                cur_sec = Section(m.no, m.title, b.page, m.how)
                cur_sec.lead = intro                 # dẫn nhập chương đi kèm mục đầu
                intro = []
                cur_ch.sections.append(cur_sec)
                eaten = eaten or m.how == "auto"     # fallback/override: block là văn bản
            elif m.kind == "subpoint":
                if cur_sec is None:
                    continue
                cur_sec.subpoints.append(Subpoint(m.title))
                eaten = True
        if eaten:
            continue

        if cur_sec is not None:
            cur_sec.add(b)
        elif cur_ch is not None:
            intro.append(b)
        else:
            warnings.append(f"tr.{b.page}: block trước chương đầu tiên bị bỏ — «{b.text[:50]}…»")

    flush_intro()
    return chapters, log


# ═══════════════════════════════════════════════════════════════
# Cắt chunk
# ═══════════════════════════════════════════════════════════════

Group = list[Block]        # đoạn kết thúc bằng ":" dính với block ngay sau


def group_blocks(blocks: list[Block]) -> list[Group]:
    groups: list[Group] = []
    i = 0
    while i < len(blocks):
        g = [blocks[i]]
        while g[-1].text.rstrip().endswith(":") and i + 1 < len(blocks):
            i += 1
            g.append(blocks[i])
        groups.append(g)
        i += 1
    return groups


@dataclass
class Unit:
    title: str | None
    groups: list[Group]

    @property
    def words(self) -> int:
        return sum(b.words for g in self.groups for b in g)


def section_units(sec: Section) -> list[Unit]:
    units: list[Unit] = []
    if sec.subpoints:
        if sec.lead:
            units.append(Unit(None, group_blocks(sec.lead)))
        for sp in sec.subpoints:
            if sp.blocks:
                units.append(Unit(sp.title, group_blocks(sp.blocks)))
    else:
        g = group_blocks(sec.lead)
        if len(g) <= 1:
            if g:
                units.append(Unit(None, g))
        else:
            units.append(Unit(None, g[:1]))       # đoạn dẫn nhập
            units.append(Unit(None, g[1:]))       # phần còn lại của mục
    return units


def merge_small(units: list[Unit], floor: int) -> list[Unit]:
    """Đơn vị < floor từ → gộp với đơn vị kế tiếp cùng mục, không có thì gộp lùi."""
    out: list[Unit] = []
    i = 0
    while i < len(units):
        u = units[i]
        if u.words < floor and i + 1 < len(units):
            nxt = units[i + 1]
            units[i + 1] = Unit(u.title or nxt.title, u.groups + nxt.groups)
            i += 1
            continue
        if u.words < floor and out:
            prev = out.pop()
            out.append(Unit(prev.title or u.title, prev.groups + u.groups))
            i += 1
            continue
        out.append(u)
        i += 1
    return out


def balanced_split(groups: list[Group], k: int, min_part: int) -> list[list[Group]] | None:
    """Chia danh sách group thành k phần liên tiếp, cân nhất có thể, mỗi phần ≥ min_part từ.
    Chỉ cắt ở ranh giới group → không bao giờ cắt giữa đoạn."""
    n = len(groups)
    if k < 2 or n < k:
        return None
    w = [sum(b.words for b in g) for g in groups]
    pre = [0]
    for x in w:
        pre.append(pre[-1] + x)
    target = pre[n] / k
    INF = float("inf")
    dp = [[INF] * (k + 1) for _ in range(n + 1)]
    back = [[-1] * (k + 1) for _ in range(n + 1)]
    dp[0][0] = 0.0
    for i in range(1, n + 1):
        for j in range(1, k + 1):
            for t in range(j - 1, i):
                if dp[t][j - 1] == INF:
                    continue
                seg = pre[i] - pre[t]
                if seg < min_part:
                    continue
                cost = dp[t][j - 1] + (seg - target) ** 2
                if cost < dp[i][j]:
                    dp[i][j] = cost
                    back[i][j] = t
    if dp[n][k] == INF:
        return None
    cuts, i, j = [], n, k
    while j > 0:
        t = back[i][j]
        cuts.append((t, i))
        i, j = t, j - 1
    cuts.reverse()
    return [groups[a:b] for a, b in cuts]


def cut_chunks(chapters: list[Chapter], cfg: dict) -> list[dict]:
    tmax = cfg["target_max"]
    split_above = cfg["split_above"]
    min_part = cfg["min_part"]
    merge_below = cfg["merge_below"]

    chunks: list[dict] = []
    seq = 0
    for ch in chapters:
        for sec in ch.sections:
            units = merge_small(section_units(sec), merge_below)
            for u in units:
                if not u.groups:
                    continue
                parts: list[list[Group]] = [u.groups]
                if u.words > split_above:
                    k = min(
                        max(2, math.ceil(u.words / tmax)),
                        max(2, u.words // min_part),
                    )
                    while k >= 2:
                        got = balanced_split(u.groups, k, min_part)
                        if got:
                            parts = got
                            break
                        k -= 1
                for idx, part in enumerate(parts, start=1):
                    blocks = [b for g in part for b in g]
                    text = "\n\n".join(b.text for b in blocks)
                    seq += 1
                    chunks.append(
                        {
                            "seq": seq,
                            "kind": "excerpt",
                            "chapter_no": ch.no,
                            "chapter_title": ch.title,
                            "section_no": sec.no,
                            "section_title": sec.title,
                            "subpoint_title": u.title,
                            "part_index": idx,
                            "part_total": len(parts),
                            "text": text,
                            "word_count": wc(text),
                            "page_from": min(b.page for b in blocks),
                            "page_to": max(b.page for b in blocks),
                            "resolved_by": sec.resolved_by,
                        }
                    )
    return chunks


# ═══════════════════════════════════════════════════════════════
# Report
# ═══════════════════════════════════════════════════════════════

def percentile(xs: list[int], p: float) -> int:
    if not xs:
        return 0
    s = sorted(xs)
    i = min(len(s) - 1, max(0, math.ceil(p * len(s)) - 1))
    return s[i]


def write_report(
    path: Path, book: dict, toc: list[TocChapter], chapters: list[Chapter],
    chunks: list[dict], log: list[str], warnings: list[str], cfg: dict, ok: bool,
) -> str:
    exp = book["expect"]
    found_sections = [s for c in chapters for s in c.sections if not s.no.endswith(".0")]
    by_how: dict[str, int] = {}
    for s in found_sections:
        by_how[s.resolved_by] = by_how.get(s.resolved_by, 0) + 1

    counts = [c["word_count"] for c in chunks]
    toc_total = sum(len(c.sections) for c in toc)
    outliers = [c for c in chunks if c["word_count"] < 150 or c["word_count"] > 450]

    toc_nos = {s.no for c in toc for s in c.sections}
    body_nos = {s.no for s in found_sections}

    L: list[str] = []
    A = L.append
    A(f"# parse_report — {book['title']}\n")
    A(f"**Kết luận: {'PASS ✅' if ok else 'FAIL ❌'}**\n")

    A("## Cấu trúc\n")
    A(f"- Chương: **{len(chapters)}/{exp['chapters']}**")
    A(f"- Mục: **{len(found_sections)}/{toc_total}** "
      f"(auto {by_how.get('auto', 0)} · page_fallback {by_how.get('page_fallback', 0)} "
      f"· override {by_how.get('override', 0)})")
    A(f"- Mục lục (ground truth) đọc được: {toc_total} mục / {len(toc)} chương")
    A(f"- Số mục theo chương — TOC: {[len(c.sections) for c in toc]}")
    A(f"- Số mục theo chương — thân sách: "
      f"{[len([s for s in c.sections if not s.no.endswith('.0')]) for c in chapters]}")
    A(f"- Kỳ vọng: {exp['sections_per_chapter']}\n")

    if toc_nos - body_nos:
        A("### ❌ Có trong mục lục, KHÔNG tìm thấy trong thân sách\n")
        for no in sorted(toc_nos - body_nos, key=lambda x: [int(p) for p in x.split(".")]):
            ts = next(s for c in toc for s in c.sections if s.no == no)
            A(f"- `{no}` «{ts.title}» (tr.{ts.page}) → thêm dòng vào `overrides.yaml`:")
            A(f"  ```yaml\n  - section: \"{no}\"\n    page: {ts.page}\n"
              f"    starts_with: \"<vài từ đầu của đoạn mở mục>\"\n  ```")
        A("")
    if body_nos - toc_nos:
        A("### ⚠️ Có trong thân sách, không có trong mục lục\n")
        for no in sorted(body_nos - toc_nos):
            A(f"- `{no}`")
        A("")

    A("## Chunk\n")
    A(f"- Tổng: **{len(chunks)}** (kỳ vọng {exp['chunks'][0]}–{exp['chunks'][1]})")
    if counts:
        A(f"- word_count — min **{min(counts)}** · median **{int(statistics.median(counts))}** "
          f"· p90 **{percentile(counts, 0.90)}** · max **{max(counts)}**")
        A(f"- Trong khoảng đích {cfg['target_min']}–{cfg['target_max']} từ: "
          f"{sum(1 for c in counts if cfg['target_min'] <= c <= cfg['target_max'])}/{len(counts)}")
    A(f"- Chunk bị cắt giữa đoạn: **0** (theo thiết kế — chỉ cắt ở ranh giới block)\n")

    A(f"### Chunk ngoài khoảng 150–450 từ ({len(outliers)})\n")
    if outliers:
        A("| seq | mục | tiểu mục | từ | trang |")
        A("|---|---|---|---|---|")
        for c in outliers:
            A(f"| {c['seq']} | {c['section_no']} | {(c['subpoint_title'] or '—')[:40]} "
              f"| {c['word_count']} | {c['page_from']}–{c['page_to']} |")
    else:
        A("_không có_")
    A("")

    A("## 3 chunk mẫu\n")
    if chunks:
        for label, c in (
            ("đầu sách", chunks[0]),
            ("giữa sách", chunks[len(chunks) // 2]),
            ("cuối sách", chunks[-1]),
        ):
            A(f"### seq {c['seq']} — {label}\n")
            A(f"`Ch.{c['chapter_no']} · {c['section_no']} {c['section_title']} · "
              f"{c['subpoint_title'] or '—'} · phần {c['part_index']}/{c['part_total']} · "
              f"{c['word_count']} từ · tr.{c['page_from']}–{c['page_to']}`\n")
            A("```")
            A(c["text"][:1200] + ("…" if len(c["text"]) > 1200 else ""))
            A("```\n")

    A(f"## Log khớp TOC ↔ thân sách\n\n```\n" + "\n".join(log) + "\n```\n")
    A(f"## Cảnh báo trích xuất ({len(warnings)})\n")
    A("```\n" + ("\n".join(warnings[:80]) if warnings else "không có") + "\n```")
    if len(warnings) > 80:
        A(f"\n_(còn {len(warnings) - 80} dòng nữa, xem stdout)_")

    out = "\n".join(L)
    path.write_text(out, encoding="utf-8")
    return out


# ═══════════════════════════════════════════════════════════════
# main
# ═══════════════════════════════════════════════════════════════

def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("pdf", type=Path, help="books/<slug>/source.pdf")
    ap.add_argument("--pad", type=float, default=1.0, help="nới bbox (pt), mặc định 1")
    args = ap.parse_args()

    pdf_path: Path = args.pdf.resolve()
    book_dir = pdf_path.parent
    if not pdf_path.exists():
        print(f"❌ Không thấy {pdf_path}", file=sys.stderr)
        return 2

    book_yaml = book_dir / "book.yaml"
    if not book_yaml.exists():
        print(f"❌ Thiếu {book_yaml}", file=sys.stderr)
        return 2
    book = yaml.safe_load(book_yaml.read_text(encoding="utf-8"))
    cfg = book.get("chunk") or {}
    cfg = {
        "target_min": cfg.get("target_min", 250),
        "target_max": cfg.get("target_max", 350),
        "split_above": cfg.get("split_above", 450),
        "min_part": cfg.get("min_part", 150),
        "merge_below": cfg.get("merge_below", 120),
    }
    ov_path = book_dir / "overrides.yaml"
    overrides = yaml.safe_load(ov_path.read_text(encoding="utf-8")) if ov_path.exists() else {}
    overrides = overrides or {}

    off = int(book.get("page_offset", 0))
    fm_a, fm_b = [p + off for p in book["pages"]["frontmatter"]]
    toc_a, toc_b = [p + off for p in book["pages"]["toc"]]
    body_a, body_b = [p + off for p in book["pages"]["body"]]

    warnings: list[str] = []
    doc = fitz.open(pdf_path)
    with pdfplumber.open(pdf_path) as pdf:
        print(f"PDF {pdf_path.name}: {len(doc)} trang")

        # ── frontmatter (tr.4–10) → nguồn cho ContextCard, không cắt chunk ──
        fm: list[str] = []
        for pno in range(fm_a, fm_b + 1):
            blocks = extract_page_blocks(doc[pno - 1], pdf.pages[pno - 1], pno, args.pad, warnings)
            h = doc[pno - 1].rect.height
            fm += [b.text for b in blocks if not is_noise(b, h)]
            pdf.pages[pno - 1].flush_cache()
        (book_dir / "frontmatter.txt").write_text("\n\n".join(fm) + "\n", encoding="utf-8")
        print(f"frontmatter.txt: {len(fm)} đoạn, {sum(wc(t) for t in fm)} từ")

        # ── mục lục = ground truth ──
        toc = parse_toc(pdf, toc_a, toc_b)
        print(f"Mục lục: {len(toc)} chương / {sum(len(c.sections) for c in toc)} mục")

        # ── thân sách ──
        body: list[Block] = []
        for pno in range(body_a, body_b + 1):
            blocks = extract_page_blocks(doc[pno - 1], pdf.pages[pno - 1], pno, args.pad, warnings)
            h = doc[pno - 1].rect.height
            body += [b for b in blocks if not is_noise(b, h)]
            pdf.pages[pno - 1].flush_cache()
    doc.close()
    print(f"Thân sách: {len(body)} block, {sum(b.words for b in body)} từ")

    chapters, log = build_tree(body, toc, overrides, warnings)
    chunks = cut_chunks(chapters, cfg)

    exp = book["expect"]
    found_sections = [s for c in chapters for s in c.sections if not s.no.endswith(".0")]
    n_auto = sum(1 for s in found_sections if s.resolved_by == "auto")
    toc_total = sum(len(c.sections) for c in toc)
    ok = (
        len(chapters) == exp["chapters"]
        and len(found_sections) == toc_total == sum(exp["sections_per_chapter"])
        and n_auto >= exp["sections_auto_min"]
        and exp["chunks"][0] <= len(chunks) <= exp["chunks"][1]
    )

    out = {
        "book": {
            "slug": book["slug"],
            "title": book["title"],
            "author_slug": book.get("author_slug"),
            "source_file": str(pdf_path.relative_to(Path.cwd())) if pdf_path.is_relative_to(Path.cwd()) else pdf_path.name,
        },
        "stats": {
            "chapters": len(chapters),
            "sections": len(found_sections),
            "sections_auto": n_auto,
            "sections_page_fallback": sum(1 for s in found_sections if s.resolved_by == "page_fallback"),
            "sections_override": sum(1 for s in found_sections if s.resolved_by == "override"),
            "chunks": len(chunks),
            "warnings": len(warnings),
        },
        "sections": [
            {"no": s.no, "title": s.title, "page": s.page, "resolved_by": s.resolved_by}
            for s in found_sections
        ],
        "chunks": chunks,
    }
    (book_dir / "chunks.json").write_text(
        json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    report = write_report(
        book_dir / "parse_report.md", book, toc, chapters, chunks, log, warnings, cfg, ok
    )
    print("\n" + report)

    if not ok:
        print(
            "\n❌ GATE A KHÔNG PASS. KHÔNG tự nới quy tắc — đọc parse_report.md, "
            "sửa overrides.yaml hoặc book.yaml rồi chạy lại.",
            file=sys.stderr,
        )
        return 1
    print("\n✅ GATE A PASS — chờ chủ dự án gõ \"OK parse\".")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
