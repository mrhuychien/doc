import { createFileRoute, useNavigate } from '@tanstack/react-router'
import { useEffect, useState } from 'react'
import { isConfigured, supabase, useSession } from '../lib/supabase'

export const Route = createFileRoute('/login')({ component: Login })

function Login() {
  const navigate = useNavigate()
  const session = useSession((s) => s.session)
  const [email, setEmail] = useState('')
  const [state, setState] = useState<'idle' | 'sending' | 'sent' | 'error'>('idle')
  const [message, setMessage] = useState('')

  useEffect(() => {
    if (session) void navigate({ to: '/' })
  }, [session, navigate])

  if (!isConfigured()) {
    return (
      <Shell>
        <p className="text-muted">
          Chưa cấu hình Supabase. Điền <code>VITE_SUPABASE_URL</code> và{' '}
          <code>VITE_SUPABASE_ANON_KEY</code> vào <code>.env.local</code> rồi chạy lại.
        </p>
      </Shell>
    )
  }

  const send = async (e: React.FormEvent) => {
    e.preventDefault()
    setState('sending')
    const { error } = await supabase().auth.signInWithOtp({
      email: email.trim().toLowerCase(),
      options: { emailRedirectTo: window.location.origin },
    })
    if (error) {
      setState('error')
      setMessage(error.message)
      return
    }
    setState('sent')
  }

  return (
    <Shell>
      {state === 'sent' ? (
        <p>
          Đã gửi link đăng nhập tới <strong>{email}</strong>. Mở mail trên chính máy này.
        </p>
      ) : (
        <form onSubmit={send} className="space-y-4">
          <label htmlFor="email" className="block font-heading text-sm text-muted">
            Email
          </label>
          <input
            id="email"
            type="email"
            required
            value={email}
            onChange={(e) => setEmail(e.target.value)}
            className="w-full border border-muted/40 bg-transparent px-3 py-2"
            autoComplete="email"
          />
          <button
            type="submit"
            disabled={state === 'sending'}
            className="w-full border border-accent/50 px-4 py-2 font-heading text-sm text-accent disabled:opacity-50"
          >
            {state === 'sending' ? 'Đang gửi…' : 'Gửi link đăng nhập'}
          </button>
          {state === 'error' ? <p className="text-sm text-muted">{message}</p> : null}
        </form>
      )}
    </Shell>
  )
}

function Shell({ children }: { children: React.ReactNode }) {
  return (
    <main className="mx-auto flex min-h-screen max-w-[640px] flex-col justify-center px-5">
      <h1 className="font-heading text-2xl font-semibold">Feed Tri Thức</h1>
      <p className="mt-1 mb-8 font-heading text-sm text-muted">
        Mỗi ngày vài ý trọn vẹn. Hết là hết.
      </p>
      {children}
    </main>
  )
}
