/**
 * Capture page for a crewmate DM whose newest history is all machinery: the
 * REAL `ChatPane` with the `crewmate` prop, over a mocked slot-detail endpoint.
 *
 * Query string:
 *   ?theme=dark|light
 *   ?scope=recent  -- the widened page still has older rows behind it, so the
 *                     hint names the rows it checked
 *   ?scope=whole   -- the widened page reaches the history's start, so the
 *                     hint makes the whole-history claim
 *
 * Driven by capture/shoot-crewmate-quiet-recent.mjs.
 */
import { createRoot } from 'react-dom/client'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { Provider } from 'react-redux'
import { MemoryRouter } from 'react-router-dom'

import { initI18n } from '../src/i18n/all'
import { store } from '../src/store'
import ChatPane from '../src/components/ChatPane'
import '../src/index.css'

const params = new URLSearchParams(location.search)
const theme = params.get('theme') === 'light' ? 'light' : 'dark'
const scope = params.get('scope') === 'whole' ? 'whole' : 'recent'
localStorage.setItem('mc-theme', theme)
document.documentElement.setAttribute('data-theme', theme === 'light' ? 'kiro-light' : 'kiro-dark')
document.documentElement.setAttribute('data-mode', theme)

const SLOT = 'member-radar'
const CREWMATE = { name: 'kirocrew-radar', label: 'Radar' }

/** One patrol cycle: a wake, a shell call and a say-nothing reply. */
const cycle = (i: number) => {
  const ts = (s: number) => new Date(Date.UTC(2026, 9, 20, 6, 0, i * 30 + s)).toISOString()
  return [
    { role: 'nudge', content: `[auto-nudge cycle ${i}]\nPatrol.`, cls: '', ts: ts(0), meta: { mid: `n${i}` } },
    { role: 'tool', content: '🔧 gh issue list --label needs-triage', cls: '', ts: ts(5), meta: { mid: `t${i}` } },
    { role: 'assistant', content: '\u200b', cls: '', ts: ts(9), meta: { mid: `a${i}` } },
  ]
}
const rows = (n: number) => Array.from({ length: n }, (_, i) => cycle(i)).flat()

const realFetch = globalThis.fetch.bind(globalThis)
globalThis.fetch = ((input: RequestInfo | URL, init?: RequestInit) => {
  const url = typeof input === 'string' ? input : input instanceof URL ? input.href : input.url
  const json = (body: unknown) => Promise.resolve(new Response(JSON.stringify(body), { status: 200, headers: { 'Content-Type': 'application/json' } }))
  if (url.includes(`/api/chat/slots/${SLOT}?`)) {
    const limit = Number(new URL(url, location.origin).searchParams.get('limit'))
    if (limit >= 500) {
      // 166 cycles = 498 rows: the widest bounded page, all machinery.
      return json(scope === 'whole'
        ? { messages: rows(166), running: false, has_more: false, total: 498 }
        : { messages: rows(166), running: false, has_more: true, next_before: 2502, total: 3000 })
    }
    return json({ messages: rows(17).slice(-50), running: false, has_more: true, next_before: 2950, total: 3000 })
  }
  if (url.includes('/api/')) {
    const body = /commands|skills|agents|models|sessions|files|artifacts/.test(url) ? '[]' : '{}'
    return Promise.resolve(new Response(body, { status: 200, headers: { 'Content-Type': 'application/json' } }))
  }
  return realFetch(input, init)
}) as typeof globalThis.fetch
const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })

function Scene() {
  return (
    <div className="bg-bg text-text flex flex-col" style={{ height: '100vh' }} data-capture-root>
      <ChatPane slotKey={SLOT} crewmate={CREWMATE} onOpenCrewWorkLog={() => {}} />
    </div>
  )
}

initI18n('en')
createRoot(document.getElementById('root')!).render(
  <Provider store={store}>
    <QueryClientProvider client={qc}>
      <MemoryRouter>
        <Scene />
      </MemoryRouter>
    </QueryClientProvider>
  </Provider>,
)
