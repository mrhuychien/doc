/**
 * Cuối feed. Không CTA, không "load more", không confetti — feed có đáy.
 * Chỉ hiện khi completed == items.length.
 */
export function EndOfDayCard({
  read,
  reviewed,
  streak,
}: {
  read: number
  reviewed: number
  streak: number
}) {
  return (
    <section className="border-t border-muted/20 px-5 py-10 text-center font-heading text-sm text-muted">
      <p>— Hết hôm nay —</p>
      <p className="mt-2">
        Đã đọc {read} · Ôn {reviewed} · Streak {streak}
      </p>
    </section>
  )
}
