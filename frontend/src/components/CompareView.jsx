import { useCallback, useEffect, useRef, useState } from 'react'
import { previewUrl } from './api.js'
import DiagnosticsOverlay from './DiagnosticsOverlay.jsx'
import Icon from './Icon.jsx'
import { Spinner } from './ui.jsx'
import { gradeOf } from './useGrading.js'

const MAX_FRAMES = 4
const MAX_ZOOM = 16
const HOME = { s: 1, x: 0, y: 0 }

/** Frames picked for side-by-side comparison on a page (up to 4, oldest dropped). */
export function useCompare() {
  const [files, setFiles] = useState([])
  const [isOpen, setOpen] = useState(false)
  return {
    files,
    isOpen: isOpen && files.length > 0,
    has: (id) => files.some(f => f.id === id),
    toggle: (file) => setFiles(fs => (fs.some(f => f.id === file.id)
      ? fs.filter(f => f.id !== file.id)
      : [...fs, file].slice(-MAX_FRAMES))),
    clear: () => { setFiles([]); setOpen(false) },
    open: () => setOpen(true),
    close: () => setOpen(false),
  }
}

/** Floating bar listing the picked frames, with the button that opens the comparison. */
export function CompareTray({ compare, onOpen }) {
  if (compare.files.length === 0) return null
  return (
    <div className="cmp-tray" role="region" aria-label="Comparison tray">
      <span className="cmp-tray-label"><Icon name="columns" size={15} />Compare</span>
      {compare.files.map(f => (
        <span key={f.id} className="af-chip" title={f.path}>
          <span>{f.filename}</span>
          <button onClick={() => compare.toggle(f)} title="Remove" aria-label={`Remove ${f.filename}`}>
            <Icon name="x" size={12} />
          </button>
        </span>
      ))}
      <button className="btn-primary btn-sm" onClick={onOpen} disabled={compare.files.length < 2}
              title={compare.files.length < 2 ? 'Pick at least two frames (compare button on a card, or C in the detail view)' : ''}>
        Compare {compare.files.length}
      </button>
      <button className="btn-ghost btn-sm" onClick={compare.clear}>Clear</button>
    </div>
  )
}

// Keep the zoomed stage covering its viewport (transform-origin is top-left)
function clampView(v, width, height) {
  const s = Math.min(MAX_ZOOM, Math.max(1, v.s))
  return {
    s,
    x: Math.min(0, Math.max(width * (1 - s), v.x)),
    y: Math.min(0, Math.max(height * (1 - s), v.y)),
  }
}

const fmt = (v, digits = 2, unit = '') => (v == null ? '—' : `${v.toFixed(digits)}${unit}`)

function Pane({ file, view, updateView, overlay, onRemove }) {
  const viewport = useRef(null)
  const img = useRef(null)
  const drag = useRef(null)
  const [state, setState] = useState('loading')   // loading | ready | failed
  const [box, setBox] = useState(null)             // image size fitted into the viewport, px

  const apply = useCallback((fn) => {
    const el = viewport.current
    if (!el) return
    const { width, height } = el.getBoundingClientRect()
    updateView(file.id, v => clampView(fn(v), width, height))
  }, [file.id, updateView])

  const fit = useCallback(() => {
    const el = viewport.current, im = img.current
    if (!el || !im || !im.naturalWidth) return
    const { width, height } = el.getBoundingClientRect()
    const k = Math.min(width / im.naturalWidth, height / im.naturalHeight)
    setBox({ w: im.naturalWidth * k, h: im.naturalHeight * k })
  }, [])

  useEffect(() => {
    window.addEventListener('resize', fit)
    return () => window.removeEventListener('resize', fit)
  }, [fit])

  // Wheel zoom around the cursor — non-passive so the page itself doesn't scroll
  useEffect(() => {
    const el = viewport.current
    if (!el) return
    const onWheel = (e) => {
      e.preventDefault()
      const rect = el.getBoundingClientRect()
      const mx = e.clientX - rect.left, my = e.clientY - rect.top
      const factor = e.deltaY < 0 ? 1.25 : 1 / 1.25
      apply(v => {
        const s = Math.min(MAX_ZOOM, Math.max(1, v.s * factor))
        const k = s / v.s
        return { s, x: mx - (mx - v.x) * k, y: my - (my - v.y) * k }
      })
    }
    el.addEventListener('wheel', onWheel, { passive: false })
    return () => el.removeEventListener('wheel', onWheel)
  }, [apply])

  const onMouseMove = (e) => {
    if (!drag.current) return
    const dx = e.clientX - drag.current.x, dy = e.clientY - drag.current.y
    drag.current = { x: e.clientX, y: e.clientY }
    apply(v => ({ ...v, x: v.x + dx, y: v.y + dy }))
  }
  const grade = gradeOf(file.rejected)

  return (
    <div className="cmp-pane">
      <div className="cmp-viewport" ref={viewport}
           onMouseDown={e => { if (e.button === 0) { drag.current = { x: e.clientX, y: e.clientY }; e.preventDefault() } }}
           onMouseMove={onMouseMove}
           onMouseUp={() => { drag.current = null }}
           onMouseLeave={() => { drag.current = null }}
           onDoubleClick={() => updateView(file.id, () => HOME)}>
        <div className="cmp-stage" style={{ transform: `translate(${view.x}px, ${view.y}px) scale(${view.s})` }}>
          <div className="thumb-wrap" style={box ? { width: box.w, height: box.h } : undefined}>
            <img ref={img} src={previewUrl(file.id)} alt={file.filename} draggable={false}
                 onLoad={() => { setState('ready'); fit() }} onError={() => setState('failed')} />
            {overlay && state === 'ready' && <DiagnosticsOverlay fileId={file.id} shadow={file.shadow} />}
          </div>
        </div>
        {state !== 'ready' && (
          <div className="cmp-status">
            {state === 'loading'
              ? <><Spinner size={14} />Rendering preview…</>
              : <><Icon name="alert" size={14} />Preview unavailable — is the file online?</>}
          </div>
        )}
      </div>
      <div className="cmp-meta">
        <div className="cmp-name" title={file.path}>
          <span>{file.filename}</span>
          <button className="icon-btn icon-btn-sm" onClick={() => onRemove(file)}
                  title="Remove from comparison" aria-label="Remove from comparison">
            <Icon name="x" size={14} />
          </button>
        </div>
        <div className="cmp-stats">
          <span>HFR <b>{fmt(file.median_hfr, 2, 'px')}</b></span>
          <span>FWHM <b>{fmt(file.psf_fwhm ?? file.median_fwhm, 2, 'px')}</b></span>
          <span>β <b>{fmt(file.psf_beta, 1)}</b></span>
          <span>Stars <b>{file.star_count?.toLocaleString() ?? '—'}</b></span>
          <span>Ecc <b>{fmt(file.eccentricity)}</b></span>
          {file.empty_cells != null && <span>Empty <b>{Math.round(file.empty_cells * 100)}%</b></span>}
          {file.streaks > 0 && <span className="cmp-warn">{file.streaks} streak{file.streaks > 1 ? 's' : ''}</span>}
          {grade && <span className={`cmp-grade ${grade}`}>{grade}</span>}
        </div>
      </div>
    </div>
  )
}

/** Side-by-side frames with zoom and pan, synchronised across panes by default. */
export default function CompareView({ files, onClose, onRemove }) {
  const [sync, setSync] = useState(true)
  const [overlay, setOverlay] = useState(false)
  const [shared, setShared] = useState(HOME)
  const [views, setViews] = useState({})           // frame id -> view, when not synced
  const syncRef = useRef(sync)
  syncRef.current = sync

  const updateView = useCallback((id, fn) => {
    if (syncRef.current) setShared(v => fn(v))
    else setViews(vs => ({ ...vs, [id]: fn(vs[id] ?? HOME) }))
  }, [])

  const toggleSync = useCallback(() => {
    // Going independent: every pane starts where the shared view was
    if (sync) setViews(Object.fromEntries(files.map(f => [f.id, shared])))
    setSync(!sync)
  }, [sync, files, shared])

  const reset = useCallback(() => { setShared(HOME); setViews({}) }, [])

  useEffect(() => {
    const handler = (e) => {
      if (e.ctrlKey || e.metaKey || e.altKey) return
      const key = e.key.toLowerCase()
      let handled = true
      if (e.key === 'Escape') onClose()
      else if (key === 's') toggleSync()
      else if (key === 'd') setOverlay(v => !v)
      else if (key === '0') reset()
      else handled = false
      if (handled) e.preventDefault()
    }
    window.addEventListener('keydown', handler)
    return () => window.removeEventListener('keydown', handler)
  }, [onClose, toggleSync, reset])

  return (
    <div className="modal-overlay" onMouseDown={e => { if (e.target === e.currentTarget) onClose() }}>
      <div className="modal cmp-modal" role="dialog" aria-modal="true" aria-label="Compare frames">
        <div className="modal-header modal-header-bordered">
          <div>
            <h2 className="modal-title">Compare {files.length} frame{files.length === 1 ? '' : 's'}</h2>
            <p className="modal-subtitle">
              Scroll to zoom · drag to pan · double-click to fit · <kbd>S</kbd> sync · <kbd>D</kbd> diagnostics · <kbd>0</kbd> reset
            </p>
          </div>
          <div className="cmp-tools">
            <button className={`tb-btn${sync ? ' active' : ''}`} onClick={toggleSync} aria-pressed={sync}
                    title="Zoom and pan every frame together (S)">
              <Icon name="columns" size={15} />Sync view
            </button>
            <button className={`tb-btn${overlay ? ' active' : ''}`} onClick={() => setOverlay(v => !v)}
                    aria-pressed={overlay} title="Star and grid diagnostics (D)">
              <Icon name="scan" size={15} />Diagnostics
            </button>
            <button className="tb-btn" onClick={reset} title="Fit every frame (0)">
              <Icon name="refresh" size={15} />Reset view
            </button>
            <span className="tb-sep" />
            <button className="icon-btn" onClick={onClose} title="Close (Esc)" aria-label="Close">
              <Icon name="x" />
            </button>
          </div>
        </div>
        <div className={`cmp-grid cmp-n${files.length}`}>
          {files.map(f => (
            <Pane key={f.id} file={f} view={sync ? shared : (views[f.id] ?? HOME)}
                  updateView={updateView} overlay={overlay} onRemove={onRemove} />
          ))}
        </div>
      </div>
    </div>
  )
}
