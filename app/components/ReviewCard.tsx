import { useState } from 'react'
import type { Chunk, Result, Stage } from '../lib/supabase'
import { Verbatim } from './ChunkCard'

const ELAPSED: Record<Stage, string> = {
  1: 'đọc hôm qua',
  2: 'đọc 7 ngày trước',
  3: 'đọc 30 ngày trước',
}

const GRADES: Array<{ key: Result; label: string }> = [
  { key: 'nho', label: 'Nhớ' },
  { key: 'mo', label: 'Mờ' },
  { key: 'quen', label: 'Quên' },
]

/**
 * Câu hỏi hiển thị = questions[stage - 1] → mỗi lần ôn một cue khác
 * (variable retrieval). Xem đáp án xong mới được chấm.
 */
export function ReviewCard({
  chunk,
  stage,
  onGrade,
}: {
  chunk: Chunk
  stage: Stage
  onGrade: (result: Result) => void
}) {
  const [revealed, setRevealed] = useState(false)
  const [showSource, setShowSource] = useState(false)
  const [graded, setGraded] = useState<Result | null>(null)

  const q = chunk.questions?.[stage - 1]
  if (!q) return null

  return (
    <article className="space-y-4 border-t border-muted/20 bg-paper px-5 py-6">
      <p className="font-heading text-xs tracking-wide text-muted">
        🔁 ÔN LẠI · {ELAPSED[stage]}
      </p>

      <p className="font-heading text-lg leading-snug">{q.cue}</p>

      {!revealed ? (
        <button
          type="button"
          onClick={() => setRevealed(true)}
          className="w-full border border-accent/50 px-4 py-2 font-heading text-sm text-accent"
        >
          Xem đáp án
        </button>
      ) : (
        <div className="space-y-4">
          <p className="border-l-2 border-accent/40 pl-3 text-base">{q.expected_answer}</p>

          <button
            type="button"
            onClick={() => setShowSource((v) => !v)}
            className="font-heading text-sm text-accent"
            aria-expanded={showSource}
          >
            {showSource ? '▾' : '▸'} Đọc lại đoạn gốc
          </button>
          {showSource ? (
            <div className="border-l-2 border-muted/30 pl-3 text-muted">
              <Verbatim text={chunk.text} />
            </div>
          ) : null}

          {graded ? (
            <p className="font-heading text-sm text-muted">
              Đã chấm: {GRADES.find((g) => g.key === graded)?.label}
            </p>
          ) : (
            <div className="grid grid-cols-3 gap-2">
              {GRADES.map((g) => (
                <button
                  key={g.key}
                  type="button"
                  onClick={() => {
                    setGraded(g.key)
                    onGrade(g.key)
                  }}
                  className="border border-muted/40 px-3 py-2 font-heading text-sm"
                >
                  {g.label}
                </button>
              ))}
            </div>
          )}
        </div>
      )}
    </article>
  )
}
