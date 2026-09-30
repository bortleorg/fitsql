import { useEffect, useState } from 'react'
import { getDiagnostics } from './api.js'

// Star codes from scanner.STAR_*: clean (measured), saturated, edge, blended
export const STAR_COLORS = ['#4ade80', '#f87171', '#facc15', '#60a5fa']
export const STAR_LABELS = ['measured', 'saturated', 'edge', 'blended']

function brightCells(bg) {
  // Outlier cells only (headlights, a lit dome) — raw lights all have some vignetting
  if (!bg || bg.length < 4) return new Set()
  const s = [...bg].sort((a, b) => a - b)
  const q1 = s[Math.floor(s.length / 4)], q3 = s[Math.floor((3 * s.length) / 4)]
  const limit = Math.max(3, q3 + 1.5 * (q3 - q1))
  return new Set(bg.flatMap((v, i) => (v > limit ? [i] : [])))
}

/**
 * Star ellipses + grid cells drawn over the preview, in original-pixel coordinates,
 * plus the frost / dew shadow when the frame is flagged (`shadow`).
 */
export default function DiagnosticsOverlay({ fileId, shadow = false }) {
  const [diag, setDiag] = useState(null)
  const [msg, setMsg] = useState('Loading diagnostics…')

  useEffect(() => {
    let live = true
    setDiag(null)
    setMsg('Loading diagnostics…')
    getDiagnostics(fileId)
      .then(r => { if (live) { setDiag(r); setMsg(null) } })
      .catch(e => { if (live) setMsg(e.message.startsWith('HTTP 404') ? 'No diagnostics yet — run Re-analyze' : e.message) })
    return () => { live = false }
  }, [fileId])

  if (!diag) return <div className="diag-legend">{msg}</div>

  const { width: w, height: h, grid: [cols, rows], cell_stars: counts, empty_below: below,
          judged, cell_bg_sigma: bg, empty_cluster: cluster = [], stars = [], streaks = [], dip = null } = diag
  const cw = w / cols, ch = h / rows
  const minR = Math.max(w, h) / 250   // tiny stars stay visible once the preview is scaled down
  const bright = brightCells(bg)
  const patch = new Set(cluster)

  return (
    <>
      <svg className="diag-svg" viewBox={`0 0 ${w} ${h}`} preserveAspectRatio="none">
        {counts.map((c, i) => {
          // The connected patch is what empty_cells measures; stray empty cells are just faint
          const fill = patch.has(i) ? 'rgba(248,113,113,0.4)'
            : judged && c < below ? 'rgba(248,113,113,0.12)'
            : bright.has(i) ? 'rgba(250,204,21,0.25)' : 'none'
          return (
            <rect key={i} x={(i % cols) * cw} y={Math.floor(i / cols) * ch} width={cw} height={ch}
                  fill={fill} stroke="rgba(255,255,255,0.15)" vectorEffect="non-scaling-stroke">
              <title>{`${c} stars${bg ? ` · background ${bg[i] >= 0 ? '+' : ''}${bg[i]}σ` : ''}`}</title>
            </rect>
          )
        })}
        {stars.map(([x, y, a, b, theta, code, used], i) => (
          <ellipse key={i} cx={x} cy={y} rx={Math.max(a * 3, minR)} ry={Math.max(b * 3, minR)}
                   transform={`rotate(${theta} ${x} ${y})`} fill="none"
                   stroke={STAR_COLORS[code] ?? '#fff'} strokeOpacity={used ? 0.95 : 0.6}
                   strokeWidth={1.2} vectorEffect="non-scaling-stroke" pointerEvents="none" />
        ))}
        {streaks.map(([x1, y1, x2, y2], i) => (
          <line key={`streak${i}`} x1={x1} y1={y1} x2={x2} y2={y2} stroke="#e879f9" strokeWidth={2.5}
                strokeDasharray="8 5" vectorEffect="non-scaling-stroke" pointerEvents="none" />
        ))}
        {shadow && dip?.box && (
          // Frost / dew shadow: where the background is at least half as deep as at its deepest
          <ellipse cx={(dip.box[0] + dip.box[2]) / 2} cy={(dip.box[1] + dip.box[3]) / 2}
                   rx={(dip.box[2] - dip.box[0]) / 2} ry={(dip.box[3] - dip.box[1]) / 2}
                   fill="rgba(56,189,248,0.1)" stroke="#38bdf8" strokeWidth={2} strokeDasharray="10 6"
                   vectorEffect="non-scaling-stroke">
            <title>{`background ${(dip.depth * 100).toFixed(1)}% darker`}</title>
          </ellipse>
        )}
      </svg>
      <div className="diag-legend">
        {STAR_LABELS.map((label, i) => (
          <span key={label}><i style={{ borderColor: STAR_COLORS[i] }} />{label}</span>
        ))}
        <span><b className="sw-empty" />empty patch</span>
        <span><b className="sw-bright" />bright cell</span>
        {streaks.length > 0 && <span><em className="sw-streak" />streak</span>}
        {shadow && dip?.box && <span><em className="sw-dip" />shadow −{(dip.depth * 100).toFixed(1)}%</span>}
        {diag.sample != null && <span>{diag.sample} measured · {diag.saturated} saturated</span>}
      </div>
    </>
  )
}
