import { createContext, useCallback, useContext, useEffect, useId, useRef, useState } from 'react'
import Icon from './Icon.jsx'
import { filterColor } from './format.js'

// navigator.clipboard only exists in secure contexts; the app is often served over plain
// http on a LAN, so fall back to a hidden textarea
export async function copyText(text) {
  if (navigator.clipboard && window.isSecureContext) {
    await navigator.clipboard.writeText(text)
    return
  }
  const ta = document.createElement('textarea')
  ta.value = text
  ta.setAttribute('readonly', '')
  ta.style.cssText = 'position:fixed;top:-1000px;opacity:0'
  document.body.appendChild(ta)
  ta.select()
  const ok = document.execCommand('copy')
  ta.remove()
  if (!ok) throw new Error('Copy failed')
}

export function Spinner({ size = 16 }) {
  return <span className="spinner" style={{ width: size, height: size }} role="status" aria-label="Loading" />
}

export function LoadingBlock({ children = 'Loading…' }) {
  return <div className="loading-block"><Spinner />{children}</div>
}

export function PageHeader({ title, subtitle, children }) {
  return (
    <header className="page-header">
      <div className="page-heading">
        <h1 className="page-title">{title}</h1>
        {subtitle && <p className="page-subtitle">{subtitle}</p>}
      </div>
      {children && <div className="page-actions">{children}</div>}
    </header>
  )
}

export function EmptyState({ icon = 'image', title, children, action }) {
  return (
    <div className="empty-state">
      <div className="empty-icon"><Icon name={icon} size={22} /></div>
      <p className="empty-title">{title}</p>
      {children && <p className="empty-text">{children}</p>}
      {action && <div className="empty-action">{action}</div>}
    </div>
  )
}

const ALERT_ICON = { danger: 'alert', warning: 'alert', success: 'check', info: 'info' }

export function Alert({ tone = 'danger', children, style }) {
  return (
    <div className={`alert alert-${tone}`} role={tone === 'danger' ? 'alert' : undefined} style={style}>
      <Icon name={ALERT_ICON[tone]} size={15} />
      <div>{children}</div>
    </div>
  )
}

export function Stat({ label, value, sub, tone }) {
  return (
    <div className={`stat${tone ? ` stat-${tone}` : ''}`}>
      <div className="stat-label">{label}</div>
      <div className="stat-value">{value}</div>
      {sub && <div className="stat-sub">{sub}</div>}
    </div>
  )
}

export function FilterChip({ name }) {
  if (!name) return <span className="text-muted">—</span>
  return <span className="filter-chip"><i style={{ background: filterColor(name) }} />{name}</span>
}

export function Pagination({ page, pages, total, perPage, onPage, disabled }) {
  if (!total) return null
  const from = (page - 1) * perPage + 1
  const to = Math.min(total, page * perPage)
  return (
    <div className="pagination">
      <span>Showing <b>{from.toLocaleString()}–{to.toLocaleString()}</b> of <b>{total.toLocaleString()}</b></span>
      {pages > 1 && (
        <div className="pagination-controls">
          <button className="btn-secondary btn-sm" onClick={() => onPage(page - 1)} disabled={disabled || page <= 1}>
            <Icon name="chevronLeft" size={14} />Prev
          </button>
          <span className="pagination-page">Page {page} of {pages}</span>
          <button className="btn-secondary btn-sm" onClick={() => onPage(page + 1)} disabled={disabled || page >= pages}>
            Next<Icon name="chevronRight" size={14} />
          </button>
        </div>
      )}
    </div>
  )
}

export function CardSkeletons({ count = 12 }) {
  return Array.from({ length: count }, (_, i) => (
    <div key={i} className="skeleton-card" aria-hidden="true">
      <div className="skeleton skeleton-thumb" />
      <div className="skeleton-lines">
        <div className="skeleton skeleton-line" style={{ width: '82%' }} />
        <div className="skeleton skeleton-line" style={{ width: '58%' }} />
        <div className="skeleton skeleton-line" style={{ width: '40%' }} />
      </div>
    </div>
  ))
}

/** Dialog shell: Esc / backdrop close, labelled for screen readers, focuses the first field. */
export function Modal({ title, subtitle, onClose, children, footer, size = 'md' }) {
  const titleId = useId()
  const ref = useRef(null)
  const closeRef = useRef(onClose)
  closeRef.current = onClose

  useEffect(() => {
    const onKey = (e) => { if (e.key === 'Escape') { e.preventDefault(); closeRef.current() } }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [])
  useEffect(() => {
    const el = ref.current
    if (el && !el.contains(document.activeElement)) {
      el.querySelector('[data-autofocus], input, select, textarea')?.focus()
    }
  }, [])

  return (
    <div className="modal-overlay" onMouseDown={e => { if (e.target === e.currentTarget) onClose() }}>
      <div ref={ref} className={`modal modal-${size}`} role="dialog" aria-modal="true" aria-labelledby={titleId}>
        <div className="modal-header">
          <div>
            <h2 id={titleId} className="modal-title">{title}</h2>
            {subtitle && <p className="modal-subtitle">{subtitle}</p>}
          </div>
          <button className="icon-btn" onClick={onClose} aria-label="Close" title="Close (Esc)">
            <Icon name="x" />
          </button>
        </div>
        <div className="modal-body">{children}</div>
        {footer && <div className="modal-footer">{footer}</div>}
      </div>
    </div>
  )
}

// ── Toasts ────────────────────────────────────────────────────────────────────
const ToastContext = createContext(() => {})
const TOAST_ICON = { success: 'check', danger: 'alert', info: 'info' }

export function ToastProvider({ children }) {
  const [toasts, setToasts] = useState([])
  const nextId = useRef(0)

  const dismiss = useCallback(id => setToasts(ts => ts.filter(t => t.id !== id)), [])
  const toast = useCallback((message, { tone = 'info', duration } = {}) => {
    const id = ++nextId.current
    setToasts(ts => [...ts.slice(-3), { id, message, tone }])
    setTimeout(() => dismiss(id), duration ?? (tone === 'danger' ? 8000 : 4000))
  }, [dismiss])

  return (
    <ToastContext.Provider value={toast}>
      {children}
      <div className="toast-stack" aria-live="polite">
        {toasts.map(t => (
          <div key={t.id} className={`toast toast-${t.tone}`} role={t.tone === 'danger' ? 'alert' : 'status'}>
            <Icon name={TOAST_ICON[t.tone]} size={15} />
            <span>{t.message}</span>
            <button className="icon-btn icon-btn-sm" onClick={() => dismiss(t.id)} aria-label="Dismiss">
              <Icon name="x" size={14} />
            </button>
          </div>
        ))}
      </div>
    </ToastContext.Provider>
  )
}

export const useToast = () => useContext(ToastContext)

// ── Confirm dialog (promise-based replacement for window.confirm) ────────────
const ConfirmContext = createContext(async () => false)

export function ConfirmProvider({ children }) {
  const [req, setReq] = useState(null)
  const confirm = useCallback(opts => new Promise(resolve => setReq({ ...opts, resolve })), [])
  const settle = (ok) => { req.resolve(ok); setReq(null) }
  const danger = req?.tone === 'danger'

  return (
    <ConfirmContext.Provider value={confirm}>
      {children}
      {req && (
        <Modal size="sm" title={req.title} onClose={() => settle(false)}
               footer={<>
                 <button className="btn-secondary" onClick={() => settle(false)} data-autofocus={danger || undefined}>
                   Cancel
                 </button>
                 <button className={danger ? 'btn-danger-solid' : 'btn-primary'} onClick={() => settle(true)}
                         data-autofocus={danger ? undefined : true}>
                   {req.confirmLabel || 'Confirm'}
                 </button>
               </>}>
          <p className="modal-text">{req.message}</p>
        </Modal>
      )}
    </ConfirmContext.Provider>
  )
}

export const useConfirm = () => useContext(ConfirmContext)
