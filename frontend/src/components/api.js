// Same-origin in production (FastAPI serves the built app). In dev, set
// VITE_API_BASE=http://localhost:8000 (see .env.development).
export const BASE = import.meta.env.VITE_API_BASE ?? ''

// "HTTP 404: <detail>" — the FastAPI `detail` when the body has one, else the raw text
async function httpError(res) {
  const text = await res.text()
  let detail = text
  try {
    const body = JSON.parse(text)
    if (body?.detail) detail = typeof body.detail === 'string' ? body.detail : JSON.stringify(body.detail)
  } catch { /* not JSON */ }
  return new Error(`HTTP ${res.status}: ${detail}`)
}

// Error message for display, without the status prefix
export const errorText = (e) => String(e?.message ?? e ?? '').replace(/^HTTP \d+:\s*/, '')

async function request(path, options = {}) {
  const res = await fetch(`${BASE}${path}`, options)
  if (!res.ok) throw await httpError(res)
  if (res.status === 204) return null
  return res.json()
}

// Paths
export const getPaths = () => request('/api/paths')
export const addPath = (path) =>
  request('/api/paths', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ path }),
  })
export const deletePath = (id) => request(`/api/paths/${id}`, { method: 'DELETE' })
export const browse = (path) =>
  request(`/api/browse${path ? `?path=${encodeURIComponent(path)}` : ''}`)

// Jobs (enqueue to worker queue)
export const triggerScan = () => request('/api/scan', { method: 'POST' })
export const triggerScanPath = (id) => request(`/api/paths/${id}/scan`, { method: 'POST' })
export const triggerReanalyze = (onlyMissing = true) =>
  request(`/api/reanalyze?only_missing=${onlyMissing}`, { method: 'POST' })
export const triggerPlatesolve = (onlyUnsolved = true) =>
  request(`/api/platesolve?only_unsolved=${onlyUnsolved}`, { method: 'POST' })

// Queue
export const getQueue = () => request('/api/queue')
export const flushQueue = () => request('/api/queue/flush', { method: 'POST' })

// Benchmark
export const benchmarkPick = (n = 10) => request(`/api/benchmark/pick?n=${n}`, { method: 'POST' })
export const benchmarkRun = (repeats = 1, dtypes = 'float32,float64') =>
  request(`/api/benchmark/run?repeats=${repeats}&dtypes=${encodeURIComponent(dtypes)}`, { method: 'POST' })
export const getBenchmark = () => request('/api/benchmark')

// Files
export const getFiles = (params = {}) => {
  const q = new URLSearchParams()
  Object.entries(params).forEach(([k, v]) => {
    if (v !== null && v !== undefined && v !== '') q.set(k, v)
  })
  return request(`/api/files?${q.toString()}`)
}
export const getFile = (id) => request(`/api/files/${id}`)
export const solveFile = (id) => request(`/api/files/${id}/solve`, { method: 'POST' })
// grade: 'accepted' | 'rejected' | null (unmark)
export const setGrade = (id, grade) =>
  request(`/api/files/${id}/grade`, {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ grade }),
  })
export const getDiagnostics = (id) => request(`/api/files/${id}/diagnostics`)
// Zoomable JPEG rendered from the FITS on first request (cached server-side)
export const previewUrl = (id, size = 2048) => `${BASE}/api/files/${id}/preview?size=${size}`
// 100% viewer: frame size (the first call reads + stretches the whole frame), then
// lossless PNG crops of native pixels. `version` busts the browser cache after a re-index.
export const getPixels = (id) => request(`/api/files/${id}/pixels`)
export const tileUrl = (id, x, y, w, h, version = '') =>
  `${BASE}/api/files/${id}/tile?x=${x}&y=${y}&w=${w}&h=${h}${version ? `&v=${encodeURIComponent(version)}` : ''}`

// Calibration coverage (lights vs darks / flats / bias)
export const getCalibration = () => request('/api/calibration')
export const solveSelection = (body) =>
  request('/api/platesolve/selection', {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  })

// Saved queries
export const getQueries = () => request('/api/queries')
export const saveQuery = (q) =>
  request('/api/queries', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(q) })
export const updateQuery = (id, q) =>
  request(`/api/queries/${id}`, { method: 'PUT', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(q) })
export const deleteQuery = (id) => request(`/api/queries/${id}`, { method: 'DELETE' })

// Stats
export const getStats = () => request('/api/stats')

// Sessions
export const getSessions = () => request('/api/sessions')

// Targets (from FITS OBJECT header)
export const getTargets = () => request('/api/targets')
export const getTarget = (name) => request(`/api/targets/${encodeURIComponent(name)}`)

// User-defined targets (matched spatially by FOV coverage)
export const getUserTargets = () => request('/api/user-targets')
export const createUserTarget = (t) =>
  request('/api/user-targets', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(t) })
export const updateUserTarget = (id, t) =>
  request(`/api/user-targets/${id}`, { method: 'PUT', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(t) })
export const deleteUserTarget = (id) => request(`/api/user-targets/${id}`, { method: 'DELETE' })
export const getUserTargetDetail = (id) => request(`/api/user-targets/${id}/detail`)
export const createTargetFromObject = (object) =>
  request(`/api/user-targets/from-object?object=${encodeURIComponent(object)}`, { method: 'POST' })

// Selector / query
export const runQuery = (body) =>
  request('/api/query', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  })

async function downloadPost(path, body, filename) {
  const res = await fetch(`${BASE}${path}`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  })
  if (!res.ok) throw await httpError(res)
  const blob = await res.blob()
  const url = URL.createObjectURL(blob)
  const a = document.createElement('a')
  a.href = url
  a.download = filename
  document.body.appendChild(a)
  a.click()
  a.remove()
  URL.revokeObjectURL(url)
}

export const exportList = (body) => downloadPost('/api/export', body, 'approved_lights.txt')
export const exportXpsm = (body, ref = 'max_fov') =>
  downloadPost(`/api/export/xpsm?ref=${encodeURIComponent(ref)}`, body, 'FastIntegration.xpsm')

// Site config
export const getSite = () => request('/api/config/site')
export const setSite = (site) =>
  request('/api/config/site', {
    method: 'PUT',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(site),
  })

// ASTAP / plate solving
export const getAstap = () => request('/api/config/astap')
export const setAstap = (path) =>
  request('/api/config/astap', {
    method: 'PUT',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ path }),
  })
export const getAstapDatabases = () => request('/api/astap/databases')

// Export path rewrite
export const getExportMap = () => request('/api/config/export-map')
export const setExportMap = (src, dst) =>
  request('/api/config/export-map', {
    method: 'PUT',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ src, dst }),
  })

// Maintenance
export const resetDatabase = () => request('/api/reset', { method: 'POST' })
export const pruneMissing = () => request('/api/prune', { method: 'POST' })
