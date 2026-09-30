import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { BASE, errorText, getDiagnostics, getPixels, tileUrl } from './api.js'
import { STAR_COLORS, STAR_LABELS } from './DiagnosticsOverlay.jsx'
import Icon from './Icon.jsx'
import { Spinner } from './ui.jsx'

const TILE = 512
const ZOOMS = [0.5, 1, 2, 4, 8]
// Corners, edges and centre — where tilt, coma and field curvature show
const PANELS = [
  ['Top left', 0, 0], ['Top', 0.5, 0], ['Top right', 1, 0],
  ['Left', 0, 0.5], ['Centre', 0.5, 0.5], ['Right', 1, 0.5],
  ['Bottom left', 0, 1], ['Bottom', 0.5, 1], ['Bottom right', 1, 1],
]

const clamp = (v, lo, hi) => Math.min(hi, Math.max(lo, v))
const pct = (z) => `${Math.round(z * 100)}%`
const keepInside = (v, info) => ({ ...v, cx: clamp(v.cx, 0, info.width), cy: clamp(v.cy, 0, info.height) })

function useElementSize(el) {
  const [size, setSize] = useState({ w: 0, h: 0 })
  useEffect(() => {
    if (!el) return
    const measure = () => setSize({ w: el.clientWidth, h: el.clientHeight })
    measure()
    const ro = new ResizeObserver(measure)
    ro.observe(el)
    return () => ro.disconnect()
  }, [el])
  return size
}

/** Star ellipses (3× the profile semi-axes) over the frame region x0..x0+w, y0..y0+h. */
function StarLayer({ stars, x0, y0, w, h, zoom, active = -1 }) {
  const minR = 5 / zoom
  return (
    <svg className="peep-stars" viewBox={`${x0} ${y0} ${w} ${h}`} preserveAspectRatio="none">
      {stars.map(([x, y, a, b, theta, code, used], i) => {
        if (x < x0 - 40 || y < y0 - 40 || x > x0 + w + 40 || y > y0 + h + 40) return null
        const cx = x + 0.5, cy = y + 0.5            // SEP centroids count from pixel centres
        return (
          <ellipse key={i} cx={cx} cy={cy} rx={Math.max(a * 3, minR)} ry={Math.max(b * 3, minR)}
                   transform={`rotate(${theta} ${cx} ${cy})`} fill="none" stroke={STAR_COLORS[code] ?? '#fff'}
                   strokeOpacity={i === active ? 1 : used ? 0.9 : 0.55} strokeWidth={i === active ? 2.5 : 1.25}
                   vectorEffect="non-scaling-stroke" />
        )
      })}
    </svg>
  )
}

/** Thumbnail with the visible region; click or drag to move the view. */
function Minimap({ file, info, x0, y0, vw, vh, onJump }) {
  if (!file.thumbnail_url) return null
  const width = 200, s = width / info.width
  const jump = (e) => {
    const r = e.currentTarget.getBoundingClientRect()
    onJump((e.clientX - r.left) / s, (e.clientY - r.top) / s)
  }
  return (
    <div className="peep-minimap" style={{ width, height: Math.round(info.height * s) }}
         title="Click or drag to move the view"
         onMouseDown={e => { e.preventDefault(); jump(e) }}
         onMouseMove={e => { if (e.buttons & 1) jump(e) }}>
      <img src={`${BASE}${file.thumbnail_url}`} alt="" draggable={false} />
      <div className="peep-minimap-view" style={{ left: x0 * s, top: y0 * s, width: vw * s, height: vh * s }} />
    </div>
  )
}

/** The frame as native-pixel tiles: drag to pan, wheel / double-click to zoom around the cursor. */
function TileView({ file, info, view, setView, stars, showStars, active, version, onCursor }) {
  const [el, setEl] = useState(null)
  const size = useElementSize(el)
  const drag = useRef(null)
  const lastWheel = useRef(0)
  const { zoom, cx, cy } = view
  // Whole screen pixels for the frame origin, so photosites stay crisp at 100% and up
  const ox = Math.round(size.w / 2 - cx * zoom), oy = Math.round(size.h / 2 - cy * zoom)
  const x0 = -ox / zoom, y0 = -oy / zoom, vw = size.w / zoom, vh = size.h / zoom

  const tiles = []
  if (size.w > 0) {
    const cols = Math.ceil(info.width / TILE), rows = Math.ceil(info.height / TILE)
    for (let r = Math.max(0, Math.floor(y0 / TILE)); r <= Math.min(rows - 1, Math.floor((y0 + vh) / TILE)); r++) {
      for (let c = Math.max(0, Math.floor(x0 / TILE)); c <= Math.min(cols - 1, Math.floor((x0 + vw) / TILE)); c++) {
        tiles.push([c, r])
      }
    }
  }

  const zoomAt = useCallback((mx, my, dir) => {
    setView(v => {
      const next = ZOOMS[clamp(ZOOMS.indexOf(v.zoom) + dir, 0, ZOOMS.length - 1)]
      if (next === v.zoom) return v
      const dx = mx - size.w / 2, dy = my - size.h / 2
      const fx = v.cx + dx / v.zoom, fy = v.cy + dy / v.zoom      // the pixel under the cursor stays put
      return keepInside({ zoom: next, cx: fx - dx / next, cy: fy - dy / next }, info)
    })
  }, [setView, size.w, size.h, info])

  // Wheel zoom — non-passive so the page doesn't scroll; one step per flick on trackpads
  useEffect(() => {
    if (!el) return
    const onWheel = (e) => {
      e.preventDefault()
      const now = performance.now()
      if (now - lastWheel.current < 90) return
      lastWheel.current = now
      const rect = el.getBoundingClientRect()
      zoomAt(e.clientX - rect.left, e.clientY - rect.top, e.deltaY < 0 ? 1 : -1)
    }
    el.addEventListener('wheel', onWheel, { passive: false })
    return () => el.removeEventListener('wheel', onWheel)
  }, [el, zoomAt])

  const onMouseMove = (e) => {
    const rect = el.getBoundingClientRect()
    const fx = x0 + (e.clientX - rect.left) / zoom, fy = y0 + (e.clientY - rect.top) / zoom
    onCursor(fx >= 0 && fy >= 0 && fx < info.width && fy < info.height ? { x: Math.floor(fx), y: Math.floor(fy) } : null)
    if (!drag.current) return
    const dx = e.clientX - drag.current.x, dy = e.clientY - drag.current.y
    drag.current = { x: e.clientX, y: e.clientY }
    setView(v => keepInside({ ...v, cx: v.cx - dx / v.zoom, cy: v.cy - dy / v.zoom }, info))
  }

  return (
    <>
      <div className="peep-viewport" ref={setEl}
           onMouseDown={e => { if (e.button === 0) { drag.current = { x: e.clientX, y: e.clientY }; e.preventDefault() } }}
           onMouseMove={onMouseMove}
           onMouseUp={() => { drag.current = null }}
           onMouseLeave={() => { drag.current = null; onCursor(null) }}
           onDoubleClick={e => { const r = el.getBoundingClientRect(); zoomAt(e.clientX - r.left, e.clientY - r.top, 1) }}>
        {tiles.map(([c, r]) => {
          const w = Math.min(TILE, info.width - c * TILE), h = Math.min(TILE, info.height - r * TILE)
          return (
            <img key={`${c}:${r}`} className={`peep-tile${zoom > 1 ? ' peep-pixelated' : ''}`} alt="" draggable={false}
                 src={tileUrl(file.id, c * TILE, r * TILE, w, h, version)}
                 style={{ left: ox + Math.round(c * TILE * zoom), top: oy + Math.round(r * TILE * zoom),
                          width: Math.round(w * zoom), height: Math.round(h * zoom) }} />
          )
        })}
        {showStars && size.w > 0 && <StarLayer stars={stars} x0={x0} y0={y0} w={vw} h={vh} zoom={zoom} active={active} />}
      </div>
      {size.w > 0 && (
        <Minimap file={file} info={info} x0={x0} y0={y0} vw={vw} vh={vh}
                 onJump={(x, y) => setView(v => keepInside({ ...v, cx: x, cy: y }, info))} />
      )}
    </>
  )
}

/** 3 × 3 crops at the chosen zoom from the corners, edges and centre (like an aberration inspector). */
function Inspector({ file, info, zoom, stars, showStars, version, onPick }) {
  const [el, setEl] = useState(null)
  const size = useElementSize(el)
  const gap = 6
  // Native pixels per panel, snapped so resizing doesn't refetch on every pixel
  const native = (px) => Math.max(16, Math.min(2048, Math.floor(px / zoom / 16) * 16))
  const nw = Math.min(native((size.w - 4 * gap) / 3), info.width)
  const nh = Math.min(native((size.h - 4 * gap) / 3), info.height)
  return (
    <div className="peep-grid" ref={setEl}>
      {size.w > 0 && PANELS.map(([label, fx, fy]) => {
        const x = Math.round(clamp(fx * info.width - nw / 2, 0, info.width - nw))
        const y = Math.round(clamp(fy * info.height - nh / 2, 0, info.height - nh))
        return (
          <button key={label} className="peep-panel" title={`${label} — open the full view here`}
                  onClick={() => onPick(x + nw / 2, y + nh / 2)}>
            <div className="peep-crop" style={{ width: nw * zoom, height: nh * zoom }}>
              <img className={zoom > 1 ? 'peep-pixelated' : undefined} src={tileUrl(file.id, x, y, nw, nh, version)}
                   alt="" draggable={false} style={{ width: nw * zoom, height: nh * zoom }} />
              {showStars && <StarLayer stars={stars} x0={x} y0={y} w={nw} h={nh} zoom={zoom} />}
            </div>
            <span className="peep-panel-label">{label}</span>
          </button>
        )
      })}
    </div>
  )
}

/**
 * Pixel peeping: the frame's native pixels at 50–800%, the measured stars (step through
 * them with N / P) and a corners / edges / centre inspector. `start` = {fx, fy}, the
 * point to open on as a fraction of the frame.
 */
export default function PixelPeep({ file, start, onClose }) {
  const [info, setInfo] = useState(null)
  const [error, setError] = useState(null)
  const [view, setView] = useState({ zoom: 1, cx: 0, cy: 0 })
  const [inspect, setInspect] = useState(false)
  const [stars, setStars] = useState([])
  const [showStars, setShowStars] = useState(false)
  const [active, setActive] = useState(-1)
  const [cursor, setCursor] = useState(null)
  const startRef = useRef(start)
  const version = file.indexed_at ?? ''

  useEffect(() => {
    let live = true
    getPixels(file.id)
      .then(r => {
        if (!live) return
        const s = startRef.current ?? { fx: 0.5, fy: 0.5 }
        setInfo(r)
        setView({ zoom: 1, cx: clamp(s.fx, 0, 1) * r.width, cy: clamp(s.fy, 0, 1) * r.height })
      })
      .catch(e => { if (live) setError(errorText(e)) })
    getDiagnostics(file.id).then(d => { if (live) setStars(d.stars ?? []) }).catch(() => { /* not analyzed */ })
    return () => { live = false }
  }, [file.id])

  // The shape-metric sample, brightest first as the analysis stored it
  const sample = useMemo(() => stars.flatMap((s, i) => (s[6] ? [i] : [])), [stars])

  const setZoom = useCallback((z) => setView(v => ({ ...v, zoom: z })), [])
  const step = useCallback((dir) => {
    if (!sample.length) return
    const pos = sample.indexOf(active)
    const next = sample[pos < 0 ? (dir > 0 ? 0 : sample.length - 1) : (pos + dir + sample.length) % sample.length]
    const [x, y] = stars[next]
    setActive(next)
    setView(v => ({ zoom: Math.max(v.zoom, 2), cx: x + 0.5, cy: y + 0.5 }))
    setInspect(false)
    setShowStars(true)
  }, [sample, stars, active])

  useEffect(() => {
    const handler = (e) => {
      if (e.ctrlKey || e.metaKey || e.altKey) return
      const key = e.key.toLowerCase()
      const panBy = (dx, dy) => { if (info) setView(v => keepInside({ ...v, cx: v.cx + dx / v.zoom, cy: v.cy + dy / v.zoom }, info)) }
      const stepZoom = (dir) => setView(v => ({ ...v, zoom: ZOOMS[clamp(ZOOMS.indexOf(v.zoom) + dir, 0, ZOOMS.length - 1)] }))
      let handled = true
      if (e.key === 'Escape') onClose()
      else if (['1', '2', '4', '8'].includes(key)) setZoom(Number(key))
      else if (key === '+' || key === '=') stepZoom(1)
      else if (key === '-') stepZoom(-1)
      else if (key === 'n') step(1)
      else if (key === 'p') step(-1)
      else if (key === 's') setShowStars(v => !v)
      else if (key === 'i') setInspect(v => !v)
      else if (key === 'arrowleft') panBy(-120, 0)
      else if (key === 'arrowright') panBy(120, 0)
      else if (key === 'arrowup') panBy(0, -120)
      else if (key === 'arrowdown') panBy(0, 120)
      else handled = false
      if (handled) e.preventDefault()
    }
    window.addEventListener('keydown', handler)
    return () => window.removeEventListener('keydown', handler)
  }, [onClose, setZoom, step, info])

  const star = active >= 0 ? stars[active] : null
  let starText = null
  if (star) {
    const [x, y, a, b, , code] = star
    const ecc = Math.sqrt(Math.max(0, 1 - (b / a) ** 2))
    starText = `Star ${sample.indexOf(active) + 1} / ${sample.length} · (${Math.round(x)}, ${Math.round(y)}) · `
      + `FWHM ≈ ${(2.3548 * Math.sqrt(a * b)).toFixed(2)} px · ecc ${ecc.toFixed(2)} · ${STAR_LABELS[code]}`
  }

  return (
    <div className="modal-overlay" onMouseDown={e => { if (e.target === e.currentTarget) onClose() }}>
      <div className="modal peep-modal" role="dialog" aria-modal="true" aria-label={`${file.filename} at full resolution`}>
        <div className="modal-header modal-header-bordered">
          <div className="peep-heading">
            <h2 className="modal-title" title={file.filename}>{file.filename}</h2>
            <p className="modal-subtitle">
              Drag to pan · scroll to zoom · <kbd>1</kbd> <kbd>2</kbd> <kbd>4</kbd> <kbd>8</kbd> zoom ·{' '}
              <kbd>N</kbd>/<kbd>P</kbd> next / previous star · <kbd>S</kbd> stars · <kbd>I</kbd> corners
            </p>
          </div>
          <div className="cmp-tools">
            <div className="peep-zooms" role="group" aria-label="Zoom">
              {ZOOMS.map(z => (
                <button key={z} className={`tb-btn${view.zoom === z ? ' active' : ''}`} onClick={() => setZoom(z)}
                        aria-pressed={view.zoom === z}>{pct(z)}</button>
              ))}
            </div>
            <span className="tb-sep" />
            <button className={`tb-btn${inspect ? ' active' : ''}`} onClick={() => setInspect(v => !v)} aria-pressed={inspect}
                    title="Corners, edges and centre side by side (I)">
              <Icon name="grid" size={15} />Corners
            </button>
            <button className={`tb-btn${showStars ? ' active' : ''}`} onClick={() => setShowStars(v => !v)}
                    aria-pressed={showStars} disabled={!stars.length}
                    title={stars.length ? 'Detected stars from the analysis (S)' : 'Not analyzed yet — run Re-analyze'}>
              <Icon name="scan" size={15} />Stars
            </button>
            <button className="tb-btn icon-only" onClick={() => step(-1)} disabled={!sample.length}
                    title="Previous measured star (P)" aria-label="Previous measured star">
              <Icon name="chevronLeft" />
            </button>
            <button className="tb-btn icon-only" onClick={() => step(1)} disabled={!sample.length}
                    title="Next measured star (N)" aria-label="Next measured star">
              <Icon name="chevronRight" />
            </button>
            <span className="tb-sep" />
            <button className="icon-btn" onClick={onClose} title="Close (Esc)" aria-label="Close">
              <Icon name="x" />
            </button>
          </div>
        </div>

        <div className="peep-body">
          {error ? (
            <div className="cmp-status"><Icon name="alert" size={14} />{error}</div>
          ) : !info ? (
            <div className="cmp-status"><Spinner size={14} />Reading the full-resolution frame…</div>
          ) : inspect ? (
            <Inspector file={file} info={info} zoom={view.zoom} stars={stars} showStars={showStars} version={version}
                       onPick={(x, y) => { setInspect(false); setView(v => ({ zoom: Math.max(v.zoom, 1), cx: x, cy: y })) }} />
          ) : (
            <TileView file={file} info={info} view={view} setView={setView} stars={stars} showStars={showStars}
                      active={active} version={version} onCursor={setCursor} />
          )}
        </div>

        <div className="peep-footer">
          <span><b>{pct(view.zoom)}</b></span>
          {info && (
            <span>{info.width.toLocaleString()} × {info.height.toLocaleString()} px
              {info.cfa && ` · raw ${info.cfa} mosaic, not debayered`}</span>
          )}
          {cursor && !inspect && <span>x <b>{cursor.x}</b> · y <b>{cursor.y}</b></span>}
          {starText && <span>{starText}</span>}
        </div>
      </div>
    </div>
  )
}
