import { useState, useEffect, useRef } from 'react'
import { getQueue, flushQueue, errorText } from './api.js'
import Icon from './Icon.jsx'
import { Spinner, useConfirm, useToast } from './ui.jsx'

function fmtEta(sec) {
  if (sec == null) return '—'
  if (sec < 60) return `${Math.round(sec)}s`
  const m = Math.floor(sec / 60)
  const s = Math.round(sec % 60)
  if (m < 60) return `${m}m ${s}s`
  const h = Math.floor(m / 60)
  return `${h}h ${m % 60}m`
}

export default function QueuePanel() {
  const [q, setQ] = useState(null)
  const [flushing, setFlushing] = useState(false)
  const timer = useRef(null)
  const toast = useToast()
  const confirm = useConfirm()

  const live = useRef(true)
  const poll = async () => { try { setQ(await getQueue()) } catch { setQ({ available: false }) } }

  // Chain polls (never overlap) — a slow /api/queue must not stack up requests
  useEffect(() => {
    live.current = true
    const loop = async () => {
      await poll()
      if (live.current) timer.current = setTimeout(loop, 2000)
    }
    loop()
    return () => { live.current = false; clearTimeout(timer.current) }
  }, [])

  const handleFlush = async () => {
    const ok = await confirm({
      title: 'Flush the job queue?',
      message: 'Pending scan, analysis and solve jobs will be cleared from the queue.',
      confirmLabel: 'Flush queue',
      tone: 'danger',
    })
    if (!ok) return
    setFlushing(true)
    try { await flushQueue(); toast('Queue flushed', { tone: 'success' }) }
    catch (e) { toast(`Flush failed: ${errorText(e)}`, { tone: 'danger' }) }
    finally { setFlushing(false); poll() }
  }

  if (!q) {
    return <div className="queue-panel"><span className="status-text"><Spinner size={13} />Checking the job queue…</span></div>
  }
  if (!q.available) {
    return (
      <div className="queue-panel">
        <span className="status-text bad"><Icon name="alert" size={14} />Job queue offline — is the Redis service running?</span>
      </div>
    )
  }

  const total = q.enqueued || 0
  const finished = (q.done || 0) + (q.failed || 0)
  const pct = total > 0 ? Math.min(100, Math.round(finished / total * 100)) : 0

  return (
    <div className="queue-panel">
      <div className="queue-row">
        <span className={`queue-state${q.active ? ' active' : ''}`}>
          <span className={`dot ${q.active ? 'dot-busy' : 'dot-idle'}`} />
          {q.active ? 'Processing' : 'Idle'}
        </span>
        <span className="queue-stat"><b>{q.workers}</b> workers</span>
        <span className="queue-stat"><b>{(q.pending ?? 0).toLocaleString()}</b> queued</span>
        <span className="queue-stat"><b>{q.inflight}</b> running</span>
        <span className="spacer" />
        <button className="btn-ghost btn-sm" onClick={handleFlush} disabled={flushing || (!q.active && q.pending === 0)}>
          {flushing ? 'Flushing…' : 'Flush queue'}
        </button>
      </div>

      {total > 0 && (
        <>
          <div className="progress" role="progressbar" aria-valuenow={pct} aria-valuemin={0} aria-valuemax={100}>
            <div className="progress-fill" style={{ width: `${pct}%` }} />
          </div>
          <div className="queue-row">
            <span className="queue-stat"><b>{(q.done || 0).toLocaleString()}</b> of {total.toLocaleString()} done ({pct}%)</span>
            {q.failed > 0 && <span className="queue-stat fail"><b>{q.failed}</b> failed</span>}
            <span className="queue-stat"><b>{q.rate_per_sec}</b>/s</span>
            <span className="queue-stat">ETA <b>{fmtEta(q.eta_sec)}</b></span>
          </div>
        </>
      )}
    </div>
  )
}
