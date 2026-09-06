import type { Book, Chunk } from '../lib/supabase'
import { Monogram, Verbatim } from './ChunkCard'

/**
 * Post 0 của mỗi cuốn: bối cảnh do LLM viết từ lời tựa + lời dịch giả.
 * Gắn nhãn "Tóm lược" rõ ràng — không giả làm trích dẫn của tác giả.
 */
export function ContextCard({
  book,
  chunk,
  onRead,
}: {
  book: Book
  chunk: Chunk
  onRead: () => void
}) {
  return (
    <article className="space-y-5 border-t border-muted/20 bg-paper px-5 py-6">
      <div className="flex gap-3">
        <Monogram text={book.author?.monogram ?? '??'} />
        <div className="font-heading text-sm leading-snug">
          <div className="font-semibold">{book.author?.name}</div>
          <div className="text-muted">{book.title}</div>
        </div>
      </div>

      <p className="inline-block border border-muted/40 px-2 py-0.5 font-heading text-xs tracking-wide text-muted">
        TÓM LƯỢC — không phải lời tác giả
      </p>

      <Verbatim text={chunk.text} />

      <button type="button" onClick={onRead} className="font-heading text-sm text-accent">
        Bắt đầu đọc →
      </button>
    </article>
  )
}
