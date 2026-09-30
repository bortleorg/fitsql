// Shared display formatters — one definition for every page.

export function fmtHours(sec) {
  if (!sec) return '0m'
  const h = Math.floor(sec / 3600)
  const m = Math.round((sec % 3600) / 60)
  return h ? `${h}h ${m}m` : `${m}m`
}

// Quality score 0-100 -> tone used for colour coding
export function qualityTone(score) {
  if (score == null) return null
  if (score >= 70) return 'good'
  if (score >= 40) return 'fair'
  return 'poor'
}

const TONE_COLOR = { good: 'var(--success)', fair: 'var(--warning)', poor: 'var(--danger)' }

export function qualityColor(score) {
  const tone = qualityTone(score)
  return tone ? TONE_COLOR[tone] : 'var(--muted)'
}

// Conventional colours for common filter names (case-insensitive)
const FILTER_COLORS = {
  L: '#d4d7e0', LUM: '#d4d7e0', CLEAR: '#d4d7e0',
  R: '#f47373', RED: '#f47373', HA: '#f47373', H: '#f47373',
  G: '#4fd18b', GREEN: '#4fd18b',
  B: '#6aa6ff', BLUE: '#6aa6ff',
  OIII: '#3fd0e0', O: '#3fd0e0', O3: '#3fd0e0',
  SII: '#f59e56', S: '#f59e56', S2: '#f59e56',
}

export function filterColor(name) {
  return FILTER_COLORS[String(name ?? '').trim().toUpperCase()] || '#9aa3ff'
}

export function fixed(v, digits = 2) {
  return v == null || Number.isNaN(Number(v)) ? null : Number(v).toFixed(digits)
}

const pad = (n, w = 2) => String(n).padStart(w, '0')

// Degrees -> "00h 44m 33.5s"
export function fmtRA(deg) {
  if (deg == null) return null
  const totalSec = (((deg % 360) + 360) % 360) / 15 * 3600
  const h = Math.floor(totalSec / 3600)
  const m = Math.floor((totalSec % 3600) / 60)
  const s = totalSec % 60
  return `${pad(h)}h ${pad(m)}m ${s.toFixed(1).padStart(4, '0')}s`
}

// Degrees -> "+41° 16′ 04″"
export function fmtDec(deg) {
  if (deg == null) return null
  const sign = deg < 0 ? '−' : '+'
  const totalSec = Math.round(Math.abs(deg) * 3600)
  const d = Math.floor(totalSec / 3600)
  const m = Math.floor((totalSec % 3600) / 60)
  const s = totalSec % 60
  return `${sign}${pad(d)}° ${pad(m)}′ ${pad(s)}″`
}

export function fmtBytes(bytes) {
  if (!bytes) return null
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(0)} KB`
  if (bytes < 1024 ** 3) return `${(bytes / 1024 / 1024).toFixed(1)} MB`
  return `${(bytes / 1024 ** 3).toFixed(2)} GB`
}

export const plural = (n, word, many = `${word}s`) => `${n} ${n === 1 ? word : many}`
