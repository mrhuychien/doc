/**
 * buildDailyPlan — dựng feed của một ngày, đúng thuật toán Blueprint §E.
 *
 * Plan được persist theo ngày: mở lại app trong ngày KHÔNG xáo lại thứ tự.
 * Ngày tính theo Asia/Ho_Chi_Minh, không dùng toISOString().
 */
import {
  supabase,
  type Book,
  type Chunk,
  type DailyPlan,
  type PlanItem,
  type Review,
  type Stage,
} from './supabase'

const TZ = 'Asia/Ho_Chi_Minh'
const fmt = new Intl.DateTimeFormat('en-CA', {
  timeZone: TZ,
  year: 'numeric',
  month: '2-digit',
  day: '2-digit',
})

/** YYYY-MM-DD theo giờ Việt Nam. */
export function todayVN(): string {
  return fmt.format(new Date())
}

/** Cộng ngày vào một chuỗi YYYY-MM-DD (không đụng tới timezone). */
export function addDays(date: string, days: number): string {
  const d = new Date(`${date}T00:00:00Z`)
  d.setUTCDate(d.getUTCDate() + days)
  return d.toISOString().slice(0, 10)
}

export async function getActiveBook(): Promise<Book | null> {
  const { data, error } = await supabase()
    .from('book')
    .select('id, slug, title, total_chunks, pace_per_day, status, author(name, monogram)')
    .eq('status', 'active')
    .order('id')
    .limit(1)
    .maybeSingle()
  if (error) throw error
  return (data as Book | null) ?? null
}

/** seq lớn nhất đã đọc — có reading_event hoặc đã sinh review. */
async function lastReadSeq(bookId: number): Promise<number> {
  const sb = supabase()
  const top = async (table: 'review' | 'reading_event') => {
    const { data, error } = await sb
      .from('chunk')
      .select(`seq, ${table}!inner(id)`)
      .eq('book_id', bookId)
      .order('seq', { ascending: false })
      .limit(1)
    if (error) throw error
    return data?.[0]?.seq ?? 0
  }
  const [a, b] = await Promise.all([top('review'), top('reading_event')])
  return Math.max(a, b)
}

/**
 * Xen kẽ 2 mới → 1 ôn, lặp lại; hết chunk mới thì dồn review còn lại vào cuối.
 */
export function interleave(news: Array<PlanItem>, reviews: Array<PlanItem>): Array<PlanItem> {
  const out: Array<PlanItem> = []
  let n = 0
  let r = 0
  while (n < news.length) {
    out.push(...news.slice(n, n + 2))
    n += 2
    if (r < reviews.length) out.push(reviews[r++])
  }
  out.push(...reviews.slice(r))
  return out
}

/**
 * Trả về plan của hôm nay. Đã có thì trả nguyên xi (không reshuffle khi reload).
 */
export async function buildDailyPlan(today: string = todayVN()): Promise<DailyPlan | null> {
  const sb = supabase()

  const existing = await sb
    .from('daily_plan')
    .select('id, plan_date, items, completed')
    .eq('plan_date', today)
    .maybeSingle()
  if (existing.error) throw existing.error
  if (existing.data) return existing.data as DailyPlan

  const book = await getActiveBook()
  if (!book) return null

  // ── review tới hạn: stage tăng dần, rồi due_on tăng dần ──
  const dueRes = await sb
    .from('review')
    .select('id, chunk_id, stage, due_on, result, answered_at')
    .is('result', null)
    .lte('due_on', today)
    .order('stage', { ascending: true })
    .order('due_on', { ascending: true })
  if (dueRes.error) throw dueRes.error
  const reviews: Array<PlanItem> = (dueRes.data as Array<Review>).map((r) => ({
    kind: 'review',
    review_id: r.id,
    chunk_id: r.chunk_id,
    stage: r.stage as Stage,
  }))

  // ── chunk mới kế tiếp theo seq ──
  const lastRead = await lastReadSeq(book.id)
  const newsRes = await sb
    .from('chunk')
    .select('id, seq')
    .eq('book_id', book.id)
    .eq('kind', 'excerpt')
    .gt('seq', lastRead)
    .order('seq', { ascending: true })
    .limit(book.pace_per_day)
  if (newsRes.error) throw newsRes.error
  const news: Array<PlanItem> = (newsRes.data as Array<Pick<Chunk, 'id' | 'seq'>>).map((c) => ({
    kind: 'chunk',
    chunk_id: c.id,
  }))

  const items: Array<PlanItem> = []
  if (lastRead === 0) {
    const ctx = await sb
      .from('chunk')
      .select('id')
      .eq('book_id', book.id)
      .eq('kind', 'context')
      .eq('seq', 0)
      .maybeSingle()
    if (ctx.error) throw ctx.error
    if (ctx.data) items.push({ kind: 'context', chunk_id: ctx.data.id })
  }
  items.push(...interleave(news, reviews))

  if (items.length === 0) return null

  const ins = await sb
    .from('daily_plan')
    .insert({ plan_date: today, items, completed: 0 })
    .select('id, plan_date, items, completed')
    .single()
  if (ins.error) {
    // Hai tab mở cùng lúc → tab sau đụng unique(plan_date). Lấy bản đã có.
    const again = await sb
      .from('daily_plan')
      .select('id, plan_date, items, completed')
      .eq('plan_date', today)
      .maybeSingle()
    if (again.data) return again.data as DailyPlan
    throw ins.error
  }
  return ins.data as DailyPlan
}

/** Nạp các chunk của plan, trả về map theo id. */
export async function loadPlanChunks(plan: DailyPlan): Promise<Map<number, Chunk>> {
  const ids = [...new Set(plan.items.map((i) => i.chunk_id))]
  if (ids.length === 0) return new Map()
  const { data, error } = await supabase().from('chunk').select('*').in('id', ids)
  if (error) throw error
  return new Map((data as Array<Chunk>).map((c) => [c.id, c]))
}

export async function markCompleted(planId: number, completed: number): Promise<void> {
  const { error } = await supabase()
    .from('daily_plan')
    .update({ completed })
    .eq('id', planId)
  if (error) throw error
}
