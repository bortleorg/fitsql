import { Fragment, useCallback, useEffect, useRef, useState } from 'react'
import { BASE, solveFile, getFile, getAstap, errorText } from './api.js'
import { gradeOf } from './useGrading.js'
import DiagnosticsOverlay from './DiagnosticsOverlay.jsx'
import Icon from './Icon.jsx'
import PixelPeep from './PixelPeep.jsx'
import { Alert, FilterChip, Spinner, copyText, useToast } from './ui.jsx'
import { fixed, fmtBytes, fmtDec, fmtRA, qualityColor } from './format.js'

const OVERLAY_KEY = 'fitsql.diagnosticsOverlay'
const MAX_UPSCALE = 2.5

function readOverlayPref() {
  try { return localStorage.getItem(OVERLAY_KEY) === '1' } catch { return false }
}

const isTyping = (el) => el && (el.tagName === 'INPUT' || el.tagName === 'TEXTAREA'
                                || el.tagName === 'SELECT' || el.isContentEditable)

const present = (v) => v !== null && v !== undefined && v !== ''

/** Label/value list; rows whose value is empty are dropped, and the section with them. */
function KvSection({ title, rows = [], children }) {
  const visible = rows.filter(([, value]) => present(value))
  if (!visible.length && !children) return null
  return (
    <section className="kv-section">
      <h3 className="kv-title">{title}</h3>
      {visible.length > 0 && (
        <dl className="kv-list">
          {visible.map(([label, value, hint]) => (
            <Fragment key={label}>
              <dt>{label}</dt>
              <dd>{value}{hint && <small>{hint}</small>}</dd>
            </Fragment>
          ))}
        </dl>
      )}
      {children}
    </section>
  )
}

function Kpi({ label, value, unit, color }) {
  return (
    <div className="kpi">
      <div className="kpi-label">{label}</div>
      {present(value)
        ? <div className="kpi-value" style={color ? { color } : undefined}>{value}{unit && <small>{unit}</small>}</div>
        : <div className="kpi-value none">—</div>}
    </div>
  )
}

// Contain-fit the preview into the stage (thumbnails are small, so allow some upscaling);
// the diagnostics SVG stretches over the same box, so it stays registered with the image
function useFitBox(stageRef, natural) {
  const [box, setBox] = useState(null)
  useEffect(() => {
    const el = stageRef.current
    if (!el || !natural) return
    const measure = () => {
      const cs = getComputedStyle(el)
      const w = el.clientWidth - parseFloat(cs.paddingLeft) - parseFloat(cs.paddingRight)
      const h = el.clientHeight - parseFloat(cs.paddingTop) - parseFloat(cs.paddingBottom)
      const k = Math.min(w / natural.w, h / natural.h, MAX_UPSCALE)
      if (k > 0) setBox({ w: Math.floor(natural.w * k), h: Math.floor(natural.h * k) })
    }
    measure()
    const ro = new ResizeObserver(measure)
    ro.observe(el)
    return () => ro.disconnect()
  }, [stageRef, natural])
  return box
}

export default function FileDetail({ file, onClose, grading, compare }) {
  const [d, setD] = useState(file)              // local copy; updates after a solve
  const [astapOk, setAstapOk] = useState(false)
  const [solving, setSolving] = useState(false)
  const [solveMsg, setSolveMsg] = useState(null) // { text, ok }
  const [overlay, setOverlay] = useState(readOverlayPref)
  const [peep, setPeep] = useState(null)        // {fx, fy} while the 100% viewer is open
  const [natural, setNatural] = useState(null)
  const pollRef = useRef(null)
  const stageRef = useRef(null)
  const box = useFitBox(stageRef, natural)
  const toast = useToast()

  // Until the sync effect below runs, `d` can still be the previous frame
  const f = d.id === file.id ? d : file
  const thumbSrc = f.thumbnail_url ? `${BASE}${f.thumbnail_url}` : null
  const grade = gradeOf(file.rejected)
  const inCompare = compare?.has(file.id)

  // Same frame re-graded: keep any fresh solve result. Different frame: start over.
  useEffect(() => {
    setD(prev => (prev && prev.id === file.id ? { ...prev, rejected: file.rejected } : file))
  }, [file])
  useEffect(() => {
    clearInterval(pollRef.current)
    setSolving(false)
    setSolveMsg(null)
    setPeep(null)
  }, [file.id])
  useEffect(() => { getAstap().then(a => setAstapOk(a.available)).catch(() => {}) }, [])
  useEffect(() => () => clearInterval(pollRef.current), [])

  const toggleOverlay = useCallback(() => {
    setOverlay(v => {
      try { localStorage.setItem(OVERLAY_KEY, v ? '0' : '1') } catch { /* private mode */ }
      return !v
    })
  }, [])

  // Keyboard: ←/J →/K navigate, A accept, X reject, U unmark, D diagnostics, Z 100% view,
  // Ctrl+Z / Ctrl+Y (or Ctrl+Shift+Z) undo/redo, Esc close. The 100% viewer has keys of its own.
  useEffect(() => {
    const handler = (e) => {
      if (peep || isTyping(e.target)) return
      const key = e.key.toLowerCase()
      const mod = e.ctrlKey || e.metaKey
      if (e.repeat && ['a', 'x', 'u', 'd', 'c', 'z', 'y'].includes(key)) return
      let handled = true
      if (e.key === 'Escape') onClose()
      else if (mod && key === 'z' && !e.shiftKey) grading?.undo()
      else if (mod && (key === 'y' || (key === 'z' && e.shiftKey))) grading?.redo()
      else if (mod || e.altKey) handled = false
      else if (key === 'arrowright' || key === 'k') grading?.move(1)
      else if (key === 'arrowleft' || key === 'j') grading?.move(-1)
      else if (key === 'a') grading?.grade('accepted')
      else if (key === 'x') grading?.grade('rejected')
      else if (key === 'u') grading?.grade(null, { advance: false })
      else if (key === 'd') toggleOverlay()
      else if (key === 'z') setPeep({ fx: 0.5, fy: 0.5 })
      else if (key === 'c' && compare) compare.toggle(file)
      else handled = false
      if (handled) e.preventDefault()
    }
    window.addEventListener('keydown', handler)
    return () => window.removeEventListener('keydown', handler)
  }, [onClose, grading, toggleOverlay, compare, file, peep])

  const handleSolve = async () => {
    if (solving) return
    setSolving(true); setSolveMsg({ text: 'Queued — solving…' })
    try {
      await solveFile(d.id)
      const started = Date.now()
      pollRef.current = setInterval(async () => {
        try {
          const fresh = await getFile(d.id)
          // Detect a new solve: solved flips on, or WCS center changed
          if (fresh.solved === 1 && (d.solved !== 1 || fresh.wcs_ra !== d.wcs_ra)) {
            setD(fresh); setSolving(false); setSolveMsg({ text: 'Solved', ok: true })
            clearInterval(pollRef.current)
          } else if (Date.now() - started > 45000) {
            setSolving(false); setSolveMsg({ text: 'No result yet — check the queue and ASTAP, then reopen later.' })
            clearInterval(pollRef.current)
          }
        } catch { /* keep polling */ }
      }, 2000)
    } catch (e) {
      setSolving(false); setSolveMsg({ text: errorText(e) })
    }
  }

  const handleCopyPath = () => {
    copyText(f.path)
      .then(() => toast('File path copied', { tone: 'success' }))
      .catch(() => toast('Could not copy to the clipboard', { tone: 'danger' }))
  }

  const measured = f.median_hfr != null || f.star_count != null || f.eccentricity != null
  const psfArcsec = f.psf_fwhm != null && f.pixel_scale != null ? fixed(f.psf_fwhm * f.pixel_scale) : null
  const binning = f.xbinning != null
    ? (f.ybinning != null && f.ybinning !== f.xbinning ? `${f.xbinning} × ${f.ybinning}` : `${f.xbinning} × ${f.xbinning}`)
    : null

  return (
    <div className="modal-overlay" onMouseDown={e => { if (e.target === e.currentTarget) onClose() }}>
      <div className="modal detail-modal" role="dialog" aria-modal="true" aria-label={f.filename}>
        <div className="detail-header">
          <div className="detail-heading">
            <h2 className="modal-title" title={f.filename}>{f.filename}</h2>
            <div className="detail-sub">
              {f.object && <span>{f.object}</span>}
              {f.filter && <FilterChip name={f.filter} />}
              {f.exptime != null && <span className="num">{f.exptime}s</span>}
              {f.date_obs && <span className="num">{f.date_obs.slice(0, 19).replace('T', ' ')} UTC</span>}
              {grade && (
                <span className={`badge badge-${grade === 'accepted' ? 'success' : 'danger'}`}>
                  <Icon name={grade === 'accepted' ? 'check' : 'x'} size={11} strokeWidth={2.5} />
                  {grade === 'accepted' ? 'Accepted' : 'Rejected'}
                </span>
              )}
            </div>
          </div>
          {solveMsg && (
            <span className={`solve-status${solveMsg.ok ? ' ok' : ''}`}>
              {solving ? <Spinner size={13} /> : solveMsg.ok ? <Icon name="check" size={14} /> : null}
              {solveMsg.text}
            </span>
          )}
          <button className="btn-secondary btn-sm" onClick={handleSolve} disabled={solving || !astapOk}
                  title={astapOk ? 'Plate solve this frame with ASTAP' : 'ASTAP is not configured (Settings)'}>
            <Icon name="crosshair" size={14} />{solving ? 'Solving…' : 'Plate solve'}
          </button>
          <button className="icon-btn" onClick={onClose} title="Close (Esc)" aria-label="Close">
            <Icon name="x" />
          </button>
        </div>

        {grading && (
          <div className="detail-toolbar">
            <button className="tb-btn icon-only" onClick={() => grading.move(-1)} disabled={grading.index <= 0}
                    title="Previous (← or J)" aria-label="Previous frame">
              <Icon name="chevronLeft" />
            </button>
            <span className="tb-pos">{grading.index + 1} / {grading.count}</span>
            <button className="tb-btn icon-only" onClick={() => grading.move(1)}
                    disabled={grading.index >= grading.count - 1} title="Next (→ or K)" aria-label="Next frame">
              <Icon name="chevronRight" />
            </button>
            <span className="tb-sep" />
            <button className={`tb-btn tb-accept${grade === 'accepted' ? ' active' : ''}`}
                    onClick={() => grading.grade('accepted')} title="Accept and go to the next frame (A)">
              <Icon name="check" size={15} />Accept
            </button>
            <button className={`tb-btn tb-reject${grade === 'rejected' ? ' active' : ''}`}
                    onClick={() => grading.grade('rejected')}
                    title="Reject and go to the next frame (X) — excluded by !rejected">
              <Icon name="x" size={15} />Reject
            </button>
            <button className="tb-btn" onClick={() => grading.grade(null, { advance: false })}
                    disabled={grade == null} title="Clear the grade (U)">
              <Icon name="minus" size={15} />Unmark
            </button>
            <span className="tb-sep" />
            <button className="tb-btn icon-only" onClick={grading.undo} disabled={!grading.canUndo}
                    title="Undo (Ctrl+Z)" aria-label="Undo">
              <Icon name="undo" size={15} />
            </button>
            <button className="tb-btn icon-only" onClick={grading.redo} disabled={!grading.canRedo}
                    title="Redo (Ctrl+Y)" aria-label="Redo">
              <Icon name="redo" size={15} />
            </button>
            {grading.error && (
              <span className="tb-error"><Icon name="alert" size={13} />{errorText(grading.error)}</span>
            )}
            <span className="spacer" />
            <button className={`tb-btn${overlay ? ' active' : ''}`} onClick={toggleOverlay} aria-pressed={overlay}
                    title="Star and grid diagnostics (D)">
              <Icon name="scan" size={15} />Diagnostics
            </button>
            <button className="tb-btn" onClick={() => setPeep({ fx: 0.5, fy: 0.5 })}
                    title="Pixel peep: native pixels, stars and corners (Z, or double-click the image)">
              <Icon name="search" size={15} />100%
            </button>
            {compare && (
              <button className={`tb-btn${inCompare ? ' active' : ''}`} onClick={() => compare.toggle(file)}
                      aria-pressed={!!inCompare} title="Add to / remove from the side-by-side comparison (C)">
                <Icon name="columns" size={15} />Compare
              </button>
            )}
          </div>
        )}

        <div className="detail-main">
          <div className="detail-stage" ref={stageRef}>
            {thumbSrc ? (
              <div className={`thumb-wrap${box ? ' fitted' : ''}`} style={box ? { width: box.w, height: box.h } : undefined}
                   title="Double-click to view this spot at 100%"
                   onDoubleClick={e => {
                     const r = e.currentTarget.getBoundingClientRect()
                     setPeep({ fx: (e.clientX - r.left) / r.width, fy: (e.clientY - r.top) / r.height })
                   }}>
                <img src={thumbSrc} alt={f.filename} draggable={false}
                     onLoad={e => setNatural({ w: e.currentTarget.naturalWidth, h: e.currentTarget.naturalHeight })} />
                {overlay && <DiagnosticsOverlay fileId={f.id} shadow={f.shadow} />}
              </div>
            ) : (
              <div className="detail-placeholder">
                <Icon name="image" size={32} />
                <span>No preview available</span>
              </div>
            )}
          </div>

          <aside className="detail-side">
            {f.index_error && <Alert>Index failed: {f.index_error}</Alert>}

            {measured && (
              <div className="detail-kpis">
                <Kpi label="Quality" value={f.quality_score != null ? Math.round(f.quality_score) : null}
                     unit="/100" color={qualityColor(f.quality_score)} />
                <Kpi label="HFR" value={fixed(f.median_hfr)} unit="px" />
                <Kpi label="FWHM" value={f.fwhm_arcsec != null ? fixed(f.fwhm_arcsec) : fixed(f.median_fwhm)}
                     unit={f.fwhm_arcsec != null ? '″' : 'px'} />
                <Kpi label="Eccentricity" value={fixed(f.eccentricity)} />
                <Kpi label="Stars" value={f.star_count != null ? f.star_count.toLocaleString() : null} />
                <Kpi label="Trail" value={fixed(f.trail_score, 3)} color={f.trailed ? 'var(--danger)' : undefined} />
              </div>
            )}

            <KvSection title="Observation" rows={[
              ['Object', f.object],
              ['Image type', f.imagetyp],
              ['Filter', f.filter ? <FilterChip name={f.filter} /> : null],
              ['Captured', f.date_obs ? `${f.date_obs.slice(0, 19).replace('T', ' ')} UTC` : null],
              ['Exposure', f.exptime != null ? `${f.exptime} s` : null],
            ]} />

            {measured && (
              <KvSection title="Frame quality" rows={[
                ['HFR', fixed(f.median_hfr), 'px'],
                f.median_fwhm != null
                  ? ['FWHM', fixed(f.median_fwhm), f.fwhm_arcsec != null ? `px · ${fixed(f.fwhm_arcsec)}″` : 'px']
                  : ['FWHM', fixed(f.fwhm_arcsec) && `${fixed(f.fwhm_arcsec)}″`],
                ['PSF FWHM (Moffat)', fixed(f.psf_fwhm), psfArcsec ? `px · ${psfArcsec}″` : 'px'],
                ['Moffat β', fixed(f.psf_beta)],
                ['Eccentricity', fixed(f.eccentricity, 3)],
                ['Trail score', fixed(f.trail_score, 3)],
                ['Empty cells', f.empty_cells != null ? `${Math.round(f.empty_cells * 100)}%` : null],
                ['Background spread', fixed(f.bg_spread, 1), 'σ'],
                ['Background dip', f.bg_dip != null ? (f.bg_dip * 100).toFixed(1) : null, '%'],
                ['Background', f.background_median != null ? Math.round(f.background_median).toLocaleString() : null, 'ADU'],
                ['Star flux', f.star_flux != null ? Math.round(f.star_flux).toLocaleString() : null, 'ADU'],
                ['Satellite streaks', f.streaks],
              ]}>
                {f.shadow && (
                  <Alert tone="warning">
                    A soft dark patch dims the background by {(f.bg_dip * 100).toFixed(1)}% — frost or dew on the sensor
                    window or a filter? The diagnostics overlay shows where.
                  </Alert>
                )}
              </KvSection>
            )}

            <KvSection title="Pointing" rows={[
              ['Acquisition RA', fmtRA(f.ra), fixed(f.ra, 4) && `${fixed(f.ra, 4)}°`],
              ['Acquisition Dec', fmtDec(f.dec), fixed(f.dec, 4) && `${fixed(f.dec, 4)}°`],
              ['Solved RA', fmtRA(f.wcs_ra), fixed(f.wcs_ra, 4) && `${fixed(f.wcs_ra, 4)}°`],
              ['Solved Dec', fmtDec(f.wcs_dec), fixed(f.wcs_dec, 4) && `${fixed(f.wcs_dec, 4)}°`],
            ]}>
              {f.coord_sep_arcmin != null && (
                f.coord_sep_arcmin > 10
                  ? <Alert tone="warning">Header pointing is {f.coord_sep_arcmin}′ from the plate solution — it looks wrong.</Alert>
                  : <span className="status-text ok"><Icon name="check" size={14} />Header pointing agrees with the solve ({f.coord_sep_arcmin}′)</span>
              )}
            </KvSection>

            {f.solved === 1 && (
              <KvSection title="Plate solution" rows={[
                ['Pixel scale', fixed(f.wcs_scale, 3), '″/px'],
                ['Rotation', fixed(f.wcs_rotation) && `${fixed(f.wcs_rotation)}°`],
                ['Field of view', f.fov_w != null && f.fov_h != null ? `${fixed(f.fov_w)}° × ${fixed(f.fov_h)}°` : null],
              ]} />
            )}

            <KvSection title="Observing conditions" rows={[
              ['Altitude', fixed(f.altitude, 1) && `${fixed(f.altitude, 1)}°`],
              ['Azimuth', fixed(f.azimuth, 1) && `${fixed(f.azimuth, 1)}°`],
              ['Airmass', fixed(f.airmass, 3)],
              ['Moon separation', fixed(f.moon_sep, 1) && `${fixed(f.moon_sep, 1)}°`],
              ['Moon illumination', f.moon_illum != null ? `${Math.round(f.moon_illum * 100)}%` : null],
              ['Moon altitude', fixed(f.moon_alt, 1) && `${fixed(f.moon_alt, 1)}°`],
            ]} />

            <KvSection title="Equipment" rows={[
              ['Telescope', f.telescop],
              ['Camera', f.instrume],
              ['Focal length', f.focal_length != null ? `${f.focal_length} mm` : null],
              ['Pixel scale', fixed(f.pixel_scale), '″/px'],
              ['Gain', f.gain],
              ['Offset', f.offset],
              ['Sensor temp', f.ccd_temp != null ? `${f.ccd_temp} °C` : null,
                f.set_temp != null ? `setpoint ${f.set_temp} °C` : null],
              ['Binning', binning],
            ]} />

            <KvSection title="File" rows={[
              ['Dimensions', f.naxis1 != null && f.naxis2 != null ? `${f.naxis1.toLocaleString()} × ${f.naxis2.toLocaleString()} px` : null],
              ['Size', fmtBytes(f.file_size)],
              ['Indexed', f.indexed_at?.slice(0, 19).replace('T', ' ')],
            ]}>
              <div className="path-box">
                <span>{f.path}</span>
                <button className="icon-btn icon-btn-sm" onClick={handleCopyPath} title="Copy path" aria-label="Copy path">
                  <Icon name="copy" size={14} />
                </button>
              </div>
            </KvSection>
          </aside>
        </div>

        {grading && (
          <div className="detail-footer" aria-label="Keyboard shortcuts">
            <span><kbd>←</kbd><kbd>→</kbd> navigate</span>
            <span><kbd>A</kbd> accept</span>
            <span><kbd>X</kbd> reject</span>
            <span><kbd>U</kbd> unmark</span>
            <span><kbd>Ctrl</kbd><kbd>Z</kbd> undo</span>
            <span><kbd>D</kbd> diagnostics</span>
            <span><kbd>Z</kbd> 100%</span>
            {compare && <span><kbd>C</kbd> compare</span>}
            <span><kbd>Esc</kbd> close</span>
          </div>
        )}
      </div>
      {peep && <PixelPeep key={f.id} file={f} start={peep} onClose={() => setPeep(null)} />}
    </div>
  )
}
