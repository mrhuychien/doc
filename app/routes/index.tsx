import { createFileRoute, useNavigate } from '@tanstack/react-router'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { useEffect, useMemo, useState } from 'react'
import { ChunkCard } from '../components/ChunkCard'
import { ContextCard } from '../components/ContextCard'
import { EndOfDayCard } from '../components/EndOfDayCard'
import { ReviewCard } from '../components/ReviewCard'
import { SettingsSheet, computeStreak } from '../components/SettingsSheet'
import {
  buildDailyPlan,
  getActiveBook,
  loadPlanChunks,
  markCompleted,
  todayVN,
} from '../lib/plan'
import { onChunkRead, onReviewGraded } from '../lib/schedule'
import { logReadingEvent } from '../lib/tracking'
import {
  isConfigured,
  supabase,
  useSession,
  type DailyPlan,
  type PlanItem,
  type Result,
} from '../lib/supabase'

export const Route = createFileRoute('/')({ component: Feed })

const keyOf = (i: PlanItem) => (i.kind === 'review' ? `r${i.review_id}` : `c${i.chunk_id}`)

/** Việc nào trong plan đã xong — suy ra từ DB để reload không mất tiến độ. */
async function loadProgress(plan: DailyPlan): Promise<Set<string>> {
  const sb = supabase()
  const chunkIds = [...new Set(plan.items.map((i) => i.chunk_id))]
  const done = new Set<string>()
  if (chunkIds.length === 0) return done

  const [rev, ev] = await Promise.all([
    sb.from('review').select('id, chunk_id, result').in('chunk_id', chunkIds),
    sb.from('reading_event').select('chunk_id').in('chunk_id', chunkIds),
  ])
  if (rev.error) throw rev.error
  if (ev.error) throw ev.error

  const graded = new Set((rev.data ?? []).filter((r) => r.result).map((r) => r.id))
  const touched = new Set([
    ...(rev.data ?? []).map((r) => r.chunk_id),
    ...(ev.data ?? []).map((r) => r.chunk_id),
  ])
  for (const item of plan.items) {
    if (item.kind === 'review' ? graded.has(item.review_id) : touched.has(item.chunk_id)) {
      done.add(keyOf(item))
    }
  }
  return done
}

function Feed() {
  const navigate = useNavigate()
  const qc = useQueryClient()
  const session = useSession((s) => s.session)
  const ready = useSession((s) => s.ready)
  const today = todayVN()
  const [done, setDone] = useState<Set<string>>(new Set())
  const [settings, setSettings] = useState(false)

  useEffect(() => {
    if (isConfigured() && ready && !session) void navigate({ to: '/login' })
  }, [ready, session, navigate])

  const enabled = isConfigured() && Boolean(session)
  const book = useQuery({ queryKey: ['book'], queryFn: getActiveBook, enabled })
  const plan = useQuery({
    queryKey: ['plan', today],
    queryFn: () => buildDailyPlan(today),
    enabled: enabled && Boolean(book.data),
  })
  const chunks = useQuery({
    queryKey: ['plan-chunks', plan.data?.id],
    queryFn: () => loadPlanChunks(plan.data as DailyPlan),
    enabled: Boolean(plan.data),
  })
  const progress = useQuery({
    queryKey: ['progress', plan.data?.id],
    queryFn: () => loadProgress(plan.data as DailyPlan),
    enabled: Boolean(plan.data),
  })
  const streak = useQuery({
    queryKey: ['streak', today],
    queryFn: async () => {
      const { data, error } = await supabase()
        .from('daily_plan')
        .select('plan_date, items, completed')
        .order('plan_date', { ascending: false })
        .limit(120)
      if (error) throw error
      return computeStreak(data ?? [], today)
    },
    enabled,
  })

  useEffect(() => {
    if (progress.data) setDone(new Set(progress.data))
  }, [progress.data])

  const complete = useMutation({
    mutationFn: async (item: PlanItem) => {
      const next = new Set(done)
      next.add(keyOf(item))
      setDone(next)
      if (plan.data) await markCompleted(plan.data.id, next.size)
      return next.size
    },
  })

  const finish = (item: PlanItem, work?: () => Promise<unknown>) => {
    void (async () => {
      try {
        if (work) await work()
        await complete.mutateAsync(item)
      } catch (err) {
        console.error(err)
      }
    })()
  }

  const items = plan.data?.items ?? []
  const counts = useMemo(() => {
    const news = items.filter((i) => i.kind === 'chunk')
    const revs = items.filter((i) => i.kind === 'review')
    return {
      newTotal: news.length,
      newDone: news.filter((i) => done.has(keyOf(i))).length,
      revTotal: revs.length,
      revDone: revs.filter((i) => done.has(keyOf(i))).length,
    }
  }, [items, done])

  if (!isConfigured()) {
    return (
      <Shell>
        <p className="text-muted">
          Chưa cấu hình Supabase. Xem <code>README.md</code> phần 2.
        </p>
      </Shell>
    )
  }
  if (!ready || book.isLoading || plan.isLoading) {
    return <Shell><p className="text-muted">Đang mở feed…</p></Shell>
  }
  const err = book.error ?? plan.error ?? chunks.error
  if (err) {
    return (
      <Shell>
        <p className="text-muted">Không đọc được dữ liệu: {(err as Error).message}</p>
        <p className="mt-2 text-sm text-muted">
          Kiểm tra email của bạn đã có trong bảng <code>allowed_user</code> (chữ thường) chưa.
        </p>
      </Shell>
    )
  }
  const activeBook = book.data
  if (!activeBook) return <Shell><p className="text-muted">Chưa có sách nào đang đọc.</p></Shell>
  if (!plan.data || items.length === 0) {
    return <Shell><p className="text-muted">Hôm nay không có gì để đọc. Hẹn mai.</p></Shell>
  }

  const map = chunks.data
  const allDone = done.size >= items.length

  return (
    <main className="mx-auto max-w-[640px] pb-16">
      <header className="sticky top-0 z-[1] flex items-center justify-between border-b border-muted/20 bg-paper px-5 py-3 font-heading text-sm">
        <span>
          Hôm nay · {today.slice(8, 10)}/{today.slice(5, 7)} ·{' '}
          <span className="text-muted">
            {counts.newDone}/{counts.newTotal} mới
            {counts.revTotal > 0 ? ` · ${counts.revDone}/${counts.revTotal} ôn` : ''}
          </span>
        </span>
        <button type="button" onClick={() => setSettings(true)} className="text-accent">
          Cài đặt
        </button>
      </header>

      {items.map((item) => {
        const chunk = map?.get(item.chunk_id)
        if (!chunk) return null
        if (item.kind === 'context') {
          return (
            <ContextCard
              key={keyOf(item)}
              book={activeBook}
              chunk={chunk}
              onRead={() => finish(item, () => logReadingEvent(chunk.id, 1))}
            />
          )
        }
        if (item.kind === 'chunk') {
          return (
            <ChunkCard
              key={keyOf(item)}
              book={activeBook}
              chunk={chunk}
              onRead={() => finish(item, () => onChunkRead(chunk.id, today))}
            />
          )
        }
        return (
          <ReviewCard
            key={keyOf(item)}
            chunk={chunk}
            stage={item.stage}
            onGrade={(result: Result) =>
              finish(item, async () => {
                await onReviewGraded(
                  { id: item.review_id, chunk_id: item.chunk_id, stage: item.stage },
                  result,
                  today,
                )
                await qc.invalidateQueries({ queryKey: ['stats', today] })
              })
            }
          />
        )
      })}

      {allDone ? (
        <EndOfDayCard
          read={counts.newDone}
          reviewed={counts.revDone}
          streak={streak.data ?? 0}
        />
      ) : null}

      {settings ? (
        <SettingsSheet book={activeBook} onClose={() => setSettings(false)} />
      ) : null}
    </main>
  )
}

function Shell({ children }: { children: React.ReactNode }) {
  return (
    <main className="mx-auto flex min-h-screen max-w-[640px] flex-col justify-center px-5">
      {children}
    </main>
  )
}
