/**
 * Lịch ôn cố định D+1 / D+7 / D+30 (Blueprint §E). Không SM-2, không FSRS.
 *
 *   nhớ : stage 1→2 due +7 | 2→3 due +30 | 3 → đóng, không tạo tiếp
 *   mờ  : giữ stage, due +3
 *   quên: về stage 1, due +1
 *
 * Mỗi lần chấm ghi `result` vào bản ghi cũ rồi INSERT bản ghi mới — giữ lịch sử
 * để tính recall theo stage.
 */
import { addDays, todayVN } from './plan'
import { supabase, type Result, type Review, type Stage } from './supabase'

export type NextReview = { stage: Stage; due_on: string } | null

/** Thuần tính toán — tách riêng để test được không cần DB. */
export function nextReview(stage: Stage, result: Result, today: string): NextReview {
  if (result === 'nho') {
    if (stage === 1) return { stage: 2, due_on: addDays(today, 7) }
    if (stage === 2) return { stage: 3, due_on: addDays(today, 30) }
    return null // stage 3 nhớ được → xong, không xếp lịch nữa
  }
  if (result === 'mo') return { stage, due_on: addDays(today, 3) }
  return { stage: 1, due_on: addDays(today, 1) } // quên
}

/** Đọc xong 1 chunk mới → xếp ôn lần đầu vào ngày mai. Đã có thì không tạo thêm. */
export async function onChunkRead(chunkId: number, today: string = todayVN()): Promise<void> {
  const sb = supabase()
  const { data, error } = await sb
    .from('review')
    .select('id')
    .eq('chunk_id', chunkId)
    .limit(1)
  if (error) throw error
  if (data && data.length > 0) return

  const ins = await sb
    .from('review')
    .insert({ chunk_id: chunkId, stage: 1, due_on: addDays(today, 1) })
  if (ins.error) throw ins.error
}

/** Chấm 1 review: đóng bản ghi cũ, mở bản ghi mới theo bảng trên. */
export async function onReviewGraded(
  review: Pick<Review, 'id' | 'chunk_id' | 'stage'>,
  result: Result,
  today: string = todayVN(),
): Promise<NextReview> {
  const sb = supabase()
  const close = await sb
    .from('review')
    .update({ result, answered_at: new Date().toISOString() })
    .eq('id', review.id)
    .is('result', null)
  if (close.error) throw close.error

  const next = nextReview(review.stage, result, today)
  if (next) {
    const ins = await sb.from('review').insert({
      chunk_id: review.chunk_id,
      stage: next.stage,
      due_on: next.due_on,
    })
    if (ins.error) throw ins.error
  }
  return next
}
