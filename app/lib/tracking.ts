/**
 * Đo thời gian đọc từng chunk — dữ liệu thô cho thí nghiệm 60 ngày.
 *
 * Bắt đầu tính khi card chiếm ≥ 60% viewport; rời viewport (hoặc rời trang) thì
 * ghi 1 reading_event. Một chunk tính "đã đọc" khi seconds ≥ 5 HOẶC người dùng
 * chạm "Ý chính".
 */
import { supabase } from './supabase'

export const READ_SECONDS = 5
const VISIBLE_RATIO = 0.6

export async function logReadingEvent(chunkId: number, seconds: number): Promise<void> {
  if (seconds <= 0) return
  const { error } = await supabase()
    .from('reading_event')
    .insert({ chunk_id: chunkId, seconds })
  if (error) throw error
}

/**
 * Gắn observer vào 1 card. `onRead` gọi đúng 1 lần, khi đã đủ READ_SECONDS.
 * Trả về hàm dọn dẹp (gọi trong cleanup của useEffect).
 */
export function trackChunk(
  el: Element,
  chunkId: number,
  onRead: () => void,
): () => void {
  let startedAt: number | null = null
  let counted = false

  const stop = () => {
    if (startedAt === null) return
    const seconds = Math.round((Date.now() - startedAt) / 1000)
    startedAt = null
    if (seconds <= 0) return
    void logReadingEvent(chunkId, seconds).catch(() => {})
    if (!counted && seconds >= READ_SECONDS) {
      counted = true
      onRead()
    }
  }

  const observer = new IntersectionObserver(
    ([entry]) => {
      if (entry.intersectionRatio >= VISIBLE_RATIO) {
        startedAt ??= Date.now()
      } else {
        stop()
      }
    },
    { threshold: [0, VISIBLE_RATIO, 1] },
  )
  observer.observe(el)

  const onHide = () => {
    if (document.visibilityState === 'hidden') stop()
  }
  document.addEventListener('visibilitychange', onHide)

  return () => {
    stop()
    observer.disconnect()
    document.removeEventListener('visibilitychange', onHide)
  }
}
