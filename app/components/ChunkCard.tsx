import { useEffect, useRef, useState } from 'react'
import type { Book, Chunk } from '../lib/supabase'
import { trackChunk } from '../lib/tracking'

const ROMAN = ['', 'I', 'II', 'III', 'IV', 'V', 'VI', 'VII', 'VIII', 'IX', 'X']

export function roman(n: number | null): string {
  return n && n > 0 && n < ROMAN.length ? ROMAN[n] : String(n ?? '')
}

export function Monogram({ text }: { text: string }) {
  return (
    <span className="flex size-11 shrink-0 items-center justify-center rounded-full border border-muted/40 font-heading text-sm tracking-wide text-muted">
      {text}
    </span>
  )
}

export function CardHeader({ book, chunk }: { book: Book; chunk: Chunk }) {
  const author = book.author?.name ?? ''
  return (
    <div className="flex gap-3">
      <Monogram text={book.author?.monogram ?? '??'} />
      <div className="min-w-0 font-heading text-sm leading-snug">
        <div className="font-semibold">{author}</div>
        <div className="text-muted">
          {book.title}
          {chunk.chapter_no ? ` · Ch.${roman(chunk.chapter_no)}` : ''}
        </div>
        {chunk.section_no ? (
          <div className="text-muted">
            {chunk.section_no} {chunk.section_title}
            {chunk.subpoint_title ? ` · ${chunk.subpoint_title}` : ''}
            {chunk.part_total > 1 ? ` · ${chunk.part_index}/${chunk.part_total}` : ''}
          </div>
        ) : null}
      </div>
    </div>
  )
}

/** Văn bản sách nguyên văn — mỗi đoạn cách nhau bằng dòng trống trong `text`. */
export function Verbatim({ text }: { text: string }) {
  return (
    <div className="space-y-4">
      {text.split('\n\n').map((p, i) => (
        <p key={i}>{p}</p>
      ))}
    </div>
  )
}

export function IdeaToggle({
  summary,
  onOpen,
}: {
  summary: string | null
  onOpen?: () => void
}) {
  const [open, setOpen] = useState(false)
  if (!summary) return null
  return (
    <div>
      <button
        type="button"
        onClick={() => {
          setOpen((v) => !v)
          if (!open) onOpen?.()
        }}
        className="font-heading text-sm text-accent"
        aria-expanded={open}
      >
        {open ? '▾' : '▸'} Ý chính
      </button>
      {open ? (
        <p className="idea-toggle mt-2 border-l-2 border-accent/40 pl-3 text-base text-muted">
          {summary}
        </p>
      ) : null}
    </div>
  )
}

export function Progress({ seq, total }: { seq: number; total: number | null }) {
  if (!total || total <= 0) return null
  const pct = Math.min(100, Math.round((seq / total) * 100))
  return (
    <div className="flex items-center gap-3 font-heading text-xs text-muted">
      <span className="h-1 flex-1 rounded-full bg-muted/25">
        <span className="block h-full rounded-full bg-accent" style={{ width: `${pct}%` }} />
      </span>
      <span>{pct}% cuốn sách</span>
    </div>
  )
}

export function ChunkCard({
  book,
  chunk,
  onRead,
}: {
  book: Book
  chunk: Chunk
  onRead: () => void
}) {
  const ref = useRef<HTMLElement | null>(null)
  const read = useRef(false)

  const markRead = () => {
    if (read.current) return
    read.current = true
    onRead()
  }

  useEffect(() => {
    if (!ref.current) return
    return trackChunk(ref.current, chunk.id, markRead)
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [chunk.id])

  return (
    <article ref={ref} className="space-y-5 border-t border-muted/20 bg-paper px-5 py-6">
      <CardHeader book={book} chunk={chunk} />
      <Verbatim text={chunk.text} />
      <IdeaToggle summary={chunk.idea_summary} onOpen={markRead} />
      <Progress seq={chunk.seq} total={book.total_chunks} />
    </article>
  )
}
