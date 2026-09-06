/**
 * Supabase client + kiểu dữ liệu + session store (Zustand).
 *
 * App chạy bằng ANON KEY + session của người dùng; RLS chặn mọi email không có
 * trong bảng allowed_user. Service role key chỉ dùng cho ingest/load.py, chạy local.
 */
import { createClient, type Session, type SupabaseClient } from '@supabase/supabase-js'
import { create } from 'zustand'

const url = import.meta.env.VITE_SUPABASE_URL as string | undefined
const anonKey = import.meta.env.VITE_SUPABASE_ANON_KEY as string | undefined

let client: SupabaseClient | null = null

/** Chỉ tạo client ở browser — SSR không có localStorage để giữ session. */
export function supabase(): SupabaseClient {
  if (typeof window === 'undefined') {
    throw new Error('supabase() chỉ gọi được ở phía client')
  }
  if (!url || !anonKey) {
    throw new Error('Thiếu VITE_SUPABASE_URL / VITE_SUPABASE_ANON_KEY trong .env.local')
  }
  client ??= createClient(url, anonKey, {
    auth: { persistSession: true, autoRefreshToken: true, detectSessionInUrl: true },
  })
  return client
}

export const isConfigured = () => Boolean(url && anonKey)

// ─── Kiểu bảng ────────────────────────────────────────────────────────
export type Question = { cue: string; expected_answer: string }

export type Chunk = {
  id: number
  book_id: number
  seq: number
  kind: 'context' | 'excerpt'
  chapter_no: number | null
  chapter_title: string | null
  section_no: string | null
  section_title: string | null
  subpoint_title: string | null
  part_index: number
  part_total: number
  text: string
  word_count: number | null
  page_from: number | null
  page_to: number | null
  idea_summary: string | null
  questions: Array<Question> | null
}

export type Book = {
  id: number
  slug: string
  title: string
  total_chunks: number | null
  pace_per_day: number
  status: string
  author: { name: string; monogram: string | null } | null
}

export type Stage = 1 | 2 | 3
export type Result = 'nho' | 'mo' | 'quen'

export type Review = {
  id: number
  chunk_id: number
  stage: Stage
  due_on: string
  result: Result | null
  answered_at: string | null
}

export type PlanItem =
  | { kind: 'context'; chunk_id: number }
  | { kind: 'chunk'; chunk_id: number }
  | { kind: 'review'; review_id: number; chunk_id: number; stage: Stage }

export type DailyPlan = {
  id: number
  plan_date: string
  items: Array<PlanItem>
  completed: number
}

// ─── Session store ────────────────────────────────────────────────────
type SessionState = {
  session: Session | null
  ready: boolean
  set: (s: Session | null) => void
}

export const useSession = create<SessionState>((set) => ({
  session: null,
  ready: false,
  set: (session) => set({ session, ready: true }),
}))

/** Gọi 1 lần khi app mount. Trả về hàm huỷ đăng ký. */
export function watchAuth(): () => void {
  const sb = supabase()
  sb.auth.getSession().then(({ data }) => useSession.getState().set(data.session))
  const { data } = sb.auth.onAuthStateChange((_e, session) =>
    useSession.getState().set(session),
  )
  return () => data.subscription.unsubscribe()
}
