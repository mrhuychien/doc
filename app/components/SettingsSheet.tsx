import { useQuery, useQueryClient } from '@tanstack/react-query'
import { useState } from 'react'
import { addDays, todayVN } from '../lib/plan'
import { supabase, type Book, type DailyPlan, type Review, type Stage } from '../lib/supabase'

export type Stats = {
  chunksRead: number
  onTimePct: number | null
  recall: Record<Stage, { total: number; nho: number }>
  streak: number
}

/** Streak = số ngày liên tiếp làm hết plan, tính lùi từ hôm nay (hoặc hôm qua). */
export function computeStreak(
  plans: Array<Pick<DailyPlan, 'plan_date' | 'items' | 'completed'>>,
  today: string,
): number {
  const done = new Map(
    plans.map((p) => [p.plan_date, p.completed >= (p.items?.length ?? 0)]),
  )
  let day = done.get(today) ? today : addDays(today, -1)
  let streak = 0
  while (done.get(day)) {
    streak += 1
    day = addDays(day, -1)
  }
  return streak
}

async function loadStats(today: string): Promise<Stats> {
  const sb = supabase()
  const since = addDays(today, -60)

  const [rev, plans] = await Promise.all([
    sb.from('review').select('chunk_id, stage, due_on, result, answered_at'),
    sb.from('daily_plan').select('plan_date, items, completed').gte('plan_date', since),
  ])
  if (rev.error) throw rev.error
  if (plans.error) throw plans.error

  const reviews = (rev.data ?? []) as Array<Review>
  const graded = reviews.filter((r) => r.result && r.answered_at)

  const recall = { 1: { total: 0, nho: 0 }, 2: { total: 0, nho: 0 }, 3: { total: 0, nho: 0 } }
  let onTime = 0
  for (const r of graded) {
    const s = recall[r.stage as Stage]
    s.total += 1
    if (r.result === 'nho') s.nho += 1
    const answeredDay = new Intl.DateTimeFormat('en-CA', {
      timeZone: 'Asia/Ho_Chi_Minh',
      year: 'numeric',
      month: '2-digit',
      day: '2-digit',
    }).format(new Date(r.answered_at as string))
    if (answeredDay <= r.due_on) onTime += 1
  }

  return {
    chunksRead: new Set(reviews.map((r) => r.chunk_id)).size,
    onTimePct: graded.length ? Math.round((onTime / graded.length) * 100) : null,
    recall,
    streak: computeStreak(
      (plans.data ?? []) as Array<Pick<DailyPlan, 'plan_date' | 'items' | 'completed'>>,
      today,
    ),
  }
}

function Row({ label, value }: { label: string; value: string }) {
  return (
    <div className="flex justify-between border-b border-muted/20 py-2 font-heading text-sm">
      <span className="text-muted">{label}</span>
      <span>{value}</span>
    </div>
  )
}

export function SettingsSheet({ book, onClose }: { book: Book; onClose: () => void }) {
  const qc = useQueryClient()
  const [pace, setPace] = useState(book.pace_per_day)
  const today = todayVN()
  const stats = useQuery({ queryKey: ['stats', today], queryFn: () => loadStats(today) })

  const savePace = async (n: number) => {
    setPace(n)
    await supabase().from('book').update({ pace_per_day: n }).eq('id', book.id)
    await qc.invalidateQueries({ queryKey: ['book'] })
  }

  const s = stats.data

  return (
    <div className="fixed inset-0 z-10 flex items-end bg-ink/30" onClick={onClose}>
      <div
        className="max-h-[85vh] w-full overflow-y-auto rounded-t-2xl bg-paper px-5 pb-8 pt-5"
        onClick={(e) => e.stopPropagation()}
      >
        <div className="mx-auto max-w-[640px]">
          <div className="flex items-center justify-between">
            <h2 className="font-heading text-lg font-semibold">Cài đặt</h2>
            <button type="button" onClick={onClose} className="font-heading text-sm text-accent">
              Đóng
            </button>
          </div>

          <div className="mt-5">
            <label htmlFor="pace" className="font-heading text-sm text-muted">
              Số đoạn mới mỗi ngày
            </label>
            <div className="mt-2 flex items-center gap-3">
              <input
                id="pace"
                type="range"
                min={1}
                max={15}
                value={pace}
                onChange={(e) => void savePace(Number(e.target.value))}
                className="flex-1 accent-accent"
              />
              <span className="w-8 text-right font-heading">{pace}</span>
            </div>
          </div>

          <h3 className="mt-7 font-heading text-sm font-semibold">60 ngày qua</h3>
          <div className="mt-2">
            {!s ? (
              <p className="py-2 font-heading text-sm text-muted">Đang tính…</p>
            ) : (
              <>
                <Row label="Đoạn đã đọc" value={`${s.chunksRead}/${book.total_chunks ?? '?'}`} />
                <Row
                  label="Ôn đúng hạn"
                  value={s.onTimePct === null ? '—' : `${s.onTimePct}%`}
                />
                {([1, 2, 3] as Array<Stage>).map((st) => (
                  <Row
                    key={st}
                    label={`Nhớ ở lần ôn ${st}`}
                    value={
                      s.recall[st].total
                        ? `${Math.round((s.recall[st].nho / s.recall[st].total) * 100)}% (${s.recall[st].total} lượt)`
                        : '—'
                    }
                  />
                ))}
                <Row label="Streak" value={`${s.streak} ngày`} />
              </>
            )}
          </div>

          <button
            type="button"
            onClick={() => void supabase().auth.signOut()}
            className="mt-7 w-full border border-muted/40 px-4 py-2 font-heading text-sm"
          >
            Đăng xuất
          </button>
        </div>
      </div>
    </div>
  )
}
