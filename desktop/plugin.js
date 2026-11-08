// 4eyes: Desktop pane and status-bar chip. Plain ES module, loaded uncompiled by Hermes Desktop (no JSX, no
// build). Talks only to this plugin's own backend (dashboard/plugin_api.py) through ctx.rest, and refreshes
// when the agent half broadcasts plugin.4eyes.* events.
import {
  Badge,
  Button,
  EmptyState,
  ScrollArea,
  StatusDot,
  queryClient,
  relativeTime,
  useQuery
} from '@hermes/plugin-sdk'
import { createElement as h } from 'react'

const KEY = ['4eyes', 'state']
let api = null // set in register(); the plugin-scoped ctx.rest

const muted = { color: 'var(--ui-text-tertiary)' }
const secondary = { color: 'var(--ui-text-secondary)' }
const line = { borderTop: '1px solid var(--ui-stroke-secondary)' }

const OUTCOME = {
  allowed: { variant: 'muted', label: 'allowed' },
  approved: { variant: 'success', label: 'you approved' },
  held: { variant: 'warn', label: 'held' },
  blocked: { variant: 'destructive', label: 'blocked' }
}

function post(path, body) {
  return api(path, { method: 'POST', body }).then(data => {
    queryClient.setQueryData(KEY, data)
    return data
  })
}

function Section({ title, children, right }) {
  return h('div', { className: 'px-3 py-2.5', style: line },
    h('div', { className: 'mb-2 flex items-center justify-between text-[0.65rem] uppercase tracking-wide', style: muted },
      h('span', null, title), right || null),
    children)
}

function Header({ state }) {
  const waiting = state.waiting.length
  const tone = state.frozen ? 'bad' : waiting ? 'warn' : 'good'
  const title = state.frozen ? 'Frozen: every payment is blocked' : waiting ? `${waiting} payment${waiting > 1 ? 's' : ''} waiting for you` : 'Guarding your wallet'
  return h('div', { className: 'flex items-center gap-2 px-3 py-2.5' },
    h(StatusDot, { tone }),
    h('div', { className: 'min-w-0 flex-1' },
      h('div', { className: 'truncate text-sm font-medium' }, title),
      h('div', { className: 'truncate text-xs', style: muted }, 'No crypto leaves without a second pair of eyes')),
    h(Button, {
      size: 'xs',
      variant: state.frozen ? 'default' : 'destructive',
      onClick: () => post('/freeze', { on: !state.frozen })
    }, state.frozen ? 'Unfreeze' : 'Freeze'))
}

function Hold({ hold }) {
  return h('div', { className: 'px-3 py-2', style: line },
    h('div', { className: 'flex items-center gap-2' },
      h(Badge, { variant: 'warn', size: 'xs' }, hold.id),
      h('span', { className: 'min-w-0 flex-1 truncate text-xs font-medium' }, hold.summary),
      h('span', { className: 'text-[0.65rem]', style: muted }, relativeTime(hold.created * 1000))),
    h('div', { className: 'mt-1.5 flex flex-col gap-1 text-xs' },
      hold.reasons.map((r, i) => h('div', { key: i, style: r.severity === 'critical' ? { color: 'var(--ui-text-primary)' } : secondary },
        (r.severity === 'critical' ? '⚠ ' : '• ') + r.text))),
    h('div', { className: 'mt-2 flex gap-1.5' },
      h(Button, { size: 'xs', variant: 'outline', onClick: () => post('/decide', { id: hold.id, decision: 'approve' }) }, 'Approve once'),
      h(Button, { size: 'xs', variant: 'ghost', onClick: () => post('/decide', { id: hold.id, decision: 'reject' }) }, 'Reject')))
}

function Entry({ item }) {
  const look = OUTCOME[item.outcome] || OUTCOME.allowed
  return h('div', { className: 'flex items-center gap-2 px-3 py-1.5', style: line },
    h(Badge, { variant: look.variant, size: 'xs' }, look.label),
    h('span', { className: 'min-w-0 flex-1 truncate text-xs', style: secondary }, item.summary),
    h('span', { className: 'text-[0.65rem]', style: muted }, item.at ? relativeTime(item.at * 1000) : ''))
}

function Pane() {
  const { data, isLoading, isError } = useQuery({ queryKey: KEY, queryFn: () => api('/state'), refetchInterval: 5000 })
  if (isLoading) return h(EmptyState, { title: 'Loading 4eyes…' })
  if (isError || !data) {
    return h(EmptyState, { title: '4eyes backend is off', description: 'Enable the 4eyes plugin in Hermes (hermes plugins enable 4eyes), then restart the gateway.' })
  }
  const limits = Object.entries(data.limits || {})
  return h(ScrollArea, { className: 'h-full' },
    h('div', { className: 'flex flex-col pb-3' },
      h(Header, { state: data }),
      h(Section, { title: 'Waiting for you' },
        data.waiting.length
          ? h('div', { style: { margin: '0 -0.75rem' } }, data.waiting.map(hold => h(Hold, { key: hold.id, hold })))
          : h('div', { className: 'text-xs', style: secondary }, 'Nothing waiting. Suspicious payments land here for you to approve or reject.')),
      h(Section, { title: 'Trusted payees' },
        data.payees.length
          ? h('div', { className: 'flex flex-col gap-1 text-xs' }, data.payees.map(p => h('div', { key: p.name, className: 'flex justify-between gap-2' },
              h('span', null, p.name), h('span', { className: 'truncate font-mono', style: muted }, p.address))))
          : h('div', { className: 'text-xs', style: secondary }, 'Add one in chat: /4eyes trust <name> <address>')),
      h(Section, { title: 'Limits (per payment / per day)' },
        h('div', { className: 'flex flex-wrap gap-x-3 gap-y-1 text-xs', style: secondary },
          limits.map(([t, v]) => h('span', { key: t }, `${t} ${v.per_tx ?? '∞'} / ${v.per_day ?? '∞'}`)))),
      h(Section, { title: 'Recent decisions' },
        data.ledger.length
          ? h('div', { style: { margin: '0 -0.75rem' } }, data.ledger.map((item, i) => h(Entry, { key: `${item.at}-${i}`, item })))
          : h('div', { className: 'text-xs', style: secondary }, 'No payments seen yet.'))))
}

function StatusChip() {
  const { data } = useQuery({ queryKey: KEY, queryFn: () => api('/state'), refetchInterval: 10000 })
  const waiting = data ? data.waiting.length : 0
  const frozen = data && data.frozen
  return h('span', { className: 'flex items-center gap-1 px-1 text-xs', style: secondary },
    h(StatusDot, { tone: !data ? 'muted' : frozen ? 'bad' : waiting ? 'warn' : 'good' }),
    !data ? '4eyes' : frozen ? '4eyes: FROZEN' : waiting ? `4eyes: ${waiting} waiting` : '4eyes: guarding')
}

export default {
  id: '4eyes',
  name: '4eyes',
  register(ctx) {
    api = ctx.rest
    const refresh = () => queryClient.invalidateQueries({ queryKey: KEY })
    for (const evt of ['checked', 'held', 'blocked', 'decided', 'state']) ctx.onEvent(`plugin.4eyes.${evt}`, refresh)
    ctx.onDispose(() => { api = null })
    ctx.register({
      id: 'pane',
      area: 'panes',
      title: '4eyes',
      data: { placement: 'right', width: '340px' },
      render: () => h(Pane, null)
    })
    ctx.register({ id: 'status', area: 'statusBar.right', render: () => h(StatusChip, null) })
  }
}
