import { useState, useEffect, useCallback, useRef } from 'react'
import {
  getPaths, addPath, deletePath, browse,
  triggerScan, triggerScanPath, triggerReanalyze, triggerPlatesolve,
  resetDatabase, pruneMissing,
  getSite, setSite, getAstap, setAstap, getAstapDatabases,
  getExportMap, setExportMap, BASE, errorText,
} from '../components/api.js'
import QueuePanel from '../components/QueuePanel.jsx'
import BenchmarkCard from '../components/BenchmarkCard.jsx'
import Icon from '../components/Icon.jsx'
import { Alert, LoadingBlock, Modal, PageHeader, Spinner, useConfirm, useToast } from '../components/ui.jsx'

const SECTIONS = [
  { id: 'paths', label: 'Library folders', icon: 'folder' },
  { id: 'jobs', label: 'Jobs & queue', icon: 'activity' },
  { id: 'site', label: 'Observing site', icon: 'pin' },
  { id: 'solving', label: 'Plate solving', icon: 'crosshair' },
  { id: 'export', label: 'Export mapping', icon: 'download' },
  { id: 'benchmark', label: 'Benchmark', icon: 'gauge' },
  { id: 'maintenance', label: 'Maintenance', icon: 'wrench' },
  { id: 'danger', label: 'Danger zone', icon: 'alert', danger: true },
  { id: 'about', label: 'About', icon: 'info' },
]
const sectionId = (id) => `settings-${id}`
const RESET_PHRASE = 'i understand'

function SettingsCard({ id, title, subtitle, danger, children, footer }) {
  return (
    <section id={sectionId(id)} className={`card settings-card${danger ? ' settings-card-danger' : ''}`}>
      <div className="card-header">
        <div>
          <h2 className="card-title">{title}</h2>
          {subtitle && <p className="card-subtitle">{subtitle}</p>}
        </div>
      </div>
      <div className="card-body">{children}</div>
      {footer && <div className="card-footer">{footer}</div>}
    </section>
  )
}

export default function SettingsPage() {
  const toast = useToast()
  const confirm = useConfirm()

  // ── Section nav (scroll spy) ──
  const bodyRef = useRef(null)
  const [active, setActive] = useState(SECTIONS[0].id)
  useEffect(() => {
    const root = bodyRef.current
    if (!root) return
    const onScroll = () => {
      const top = root.getBoundingClientRect().top
      let current = SECTIONS[0].id
      for (const s of SECTIONS) {
        const el = document.getElementById(sectionId(s.id))
        if (el && el.getBoundingClientRect().top - top <= 96) current = s.id
      }
      if (root.scrollTop + root.clientHeight >= root.scrollHeight - 4) current = SECTIONS[SECTIONS.length - 1].id
      setActive(current)
    }
    onScroll()
    root.addEventListener('scroll', onScroll, { passive: true })
    return () => root.removeEventListener('scroll', onScroll)
  }, [])
  const jump = (id) => document.getElementById(sectionId(id))?.scrollIntoView({ behavior: 'smooth', block: 'start' })

  // ── Watched paths ──
  const [paths, setPaths] = useState([])
  const [pathsLoaded, setPathsLoaded] = useState(false)
  const [newPath, setNewPath] = useState('')
  const [addError, setAddError] = useState(null)

  const fetchPaths = useCallback(async () => {
    try { setPaths(await getPaths()) } catch { /* ignore */ } finally { setPathsLoaded(true) }
  }, [])
  useEffect(() => { fetchPaths() }, [fetchPaths])

  const handleAddPath = async (pathArg) => {
    const trimmed = (typeof pathArg === 'string' ? pathArg : newPath).trim()
    if (!trimmed) return false
    setAddError(null)
    try {
      await addPath(trimmed)
      setNewPath('')
      await fetchPaths()
      toast(`Watching ${trimmed}`, { tone: 'success' })
      return true
    } catch (e) { setAddError(errorText(e)); return false }
  }
  const handleDeletePath = async (p) => {
    const ok = await confirm({
      title: 'Stop watching this folder?',
      message: `${p.path} will be removed from the watched folders.`,
      confirmLabel: 'Remove folder',
      tone: 'danger',
    })
    if (!ok) return
    try { await deletePath(p.id); await fetchPaths() } catch (e) { toast(errorText(e), { tone: 'danger' }) }
  }

  // ── Folder picker ──
  const [pickerOpen, setPickerOpen] = useState(false)
  const [pickerData, setPickerData] = useState(null)
  const [pickerLoading, setPickerLoading] = useState(false)
  const [pickerError, setPickerError] = useState(null)
  const navigate = useCallback(async (path) => {
    setPickerLoading(true); setPickerError(null)
    try { setPickerData(await browse(path)) } catch (e) { setPickerError(errorText(e)) } finally { setPickerLoading(false) }
  }, [])
  const openPicker = () => { setPickerOpen(true); navigate(null) }
  const addFromPicker = async () => {
    if (!pickerData?.path) return
    if (await handleAddPath(pickerData.path)) setPickerOpen(false)
  }

  // ── Jobs (enqueue) ──
  const runJob = async (fn, label) => {
    try {
      const r = await fn()
      toast(r?.message || `${label} queued`, { tone: 'success' })
    } catch (e) {
      toast(`${label} failed: ${errorText(e)}`, { tone: 'danger' })
    }
  }
  const reanalyzeAll = async () => {
    const ok = await confirm({
      title: 'Re-analyze every light frame?',
      message: 'Recomputes every frame with the current settings. Slow, but makes the whole catalog consistent.',
      confirmLabel: 'Re-analyze all',
    })
    if (ok) runJob(() => triggerReanalyze(false), 'Re-analyze all')
  }
  const resolveAll = async () => {
    const ok = await confirm({
      title: 'Re-solve every light frame?',
      message: 'Verifies acquisition RA/Dec against ASTAP and flags mismatches (coord_sep). Slow.',
      confirmLabel: 'Re-solve all',
    })
    if (ok) runJob(() => triggerPlatesolve(false), 'Plate solve all')
  }

  // ── Default site ──
  const [site, setSiteState] = useState({ lat: '', lon: '', elev: '' })
  useEffect(() => {
    getSite().then(s => setSiteState({ lat: s.lat ?? '', lon: s.lon ?? '', elev: s.elev ?? '' })).catch(() => {})
  }, [])
  const handleSaveSite = async () => {
    try {
      await setSite({
        lat: site.lat === '' ? null : parseFloat(site.lat),
        lon: site.lon === '' ? null : parseFloat(site.lon),
        elev: site.elev === '' ? null : parseFloat(site.elev),
      })
      toast('Observing site saved — re-analyze frames to apply it', { tone: 'success' })
    } catch (e) { toast(`Couldn't save the site: ${errorText(e)}`, { tone: 'danger' }) }
  }

  // ── ASTAP ──
  const [astap, setAstapState] = useState({ path: '', available: false })
  const refreshAstap = useCallback(() => {
    getAstap().then(a => setAstapState({ path: a.path ?? '', available: a.available })).catch(() => {})
  }, [])
  useEffect(() => { refreshAstap() }, [refreshAstap])
  const handleSaveAstap = async () => {
    try {
      const a = await setAstap(astap.path.trim() || null)
      setAstapState({ path: a.path ?? '', available: a.available })
      toast(a.available ? 'ASTAP path saved — ready to solve' : 'ASTAP path saved', { tone: 'success' })
    } catch (e) { toast(`Couldn't save the ASTAP path: ${errorText(e)}`, { tone: 'danger' }) }
  }

  // ── Star database download (SSE — a download, not a queue job) ──
  const [dbs, setDbs] = useState(null)
  const [dbDownloading, setDbDownloading] = useState(null)
  const [dbProgress, setDbProgress] = useState(null)
  const dbEsRef = useRef(null)
  const refreshDbs = useCallback(() => { getAstapDatabases().then(setDbs).catch(() => {}) }, [])
  useEffect(() => { refreshDbs() }, [refreshDbs])
  useEffect(() => () => dbEsRef.current?.close(), [])

  const handleDownloadDb = (key) => {
    if (dbDownloading) return
    setDbDownloading(key)
    setDbProgress({ phase: 'start' })
    const es = new EventSource(`${BASE}/api/astap/download/stream?db=${encodeURIComponent(key)}`)
    dbEsRef.current = es
    es.onmessage = (e) => {
      const ev = JSON.parse(e.data)
      if (ev.type === 'progress') setDbProgress(ev)
      else if (ev.type === 'done') {
        setDbProgress({ phase: 'done' }); setDbDownloading(null); es.close(); refreshDbs(); refreshAstap()
        toast('Star database installed', { tone: 'success' })
      } else if (ev.type === 'error') {
        setDbProgress({ phase: 'error', message: ev.message }); setDbDownloading(null); es.close()
      }
    }
    es.onerror = () => { setDbDownloading(null); es.close() }
  }

  // ── Export path rewrite ──
  const [exportMap, setExportMapState] = useState({ src: '', dst: '' })
  useEffect(() => {
    getExportMap().then(m => setExportMapState({ src: m.src ?? '', dst: m.dst ?? '' })).catch(() => {})
  }, [])
  const handleSaveExportMap = async () => {
    try {
      await setExportMap(exportMap.src.trim() || null, exportMap.dst.trim() || null)
      toast('Export mapping saved', { tone: 'success' })
    } catch (e) { toast(`Couldn't save the mapping: ${errorText(e)}`, { tone: 'danger' }) }
  }

  // ── Prune / reset ──
  const [pruning, setPruning] = useState(false)
  const handlePrune = async () => {
    const ok = await confirm({
      title: 'Prune missing files?',
      message: 'Catalog entries whose files can no longer be found on disk will be removed. Make sure your library folders are reachable first.',
      confirmLabel: 'Prune entries',
      tone: 'danger',
    })
    if (!ok) return
    setPruning(true)
    try {
      const removed = (await pruneMissing()).removed
      toast(`Removed ${removed} catalog entr${removed === 1 ? 'y' : 'ies'}`, { tone: 'success' })
    } catch (e) { toast(`Prune failed: ${errorText(e)}`, { tone: 'danger' }) } finally { setPruning(false) }
  }

  const [resetConfirm, setResetConfirm] = useState('')
  const [resetting, setResetting] = useState(false)
  const handleReset = async () => {
    if (resetConfirm !== RESET_PHRASE) return
    setResetting(true)
    try {
      await resetDatabase()
      setResetConfirm('')
      await fetchPaths()
      toast('Database reset. All watched folders and indexed files were cleared.', { tone: 'success' })
    } catch (e) { toast(`Reset failed: ${errorText(e)}`, { tone: 'danger' }) } finally { setResetting(false) }
  }

  const dlPct = dbProgress?.pct

  return (
    <div className="page">
      <PageHeader title="Settings" subtitle="Library folders, background jobs, plate solving and maintenance" />

      <div className="page-body" ref={bodyRef}>
        <div className="settings-layout">
          <nav className="settings-nav" aria-label="Settings sections">
            {SECTIONS.map(s => (
              <button key={s.id} className={`${s.danger ? 'danger ' : ''}${active === s.id ? 'active' : ''}`}
                      aria-current={active === s.id ? 'true' : undefined}
                      onClick={() => { setActive(s.id); jump(s.id) }}>
                <Icon name={s.icon} size={15} />{s.label}
              </button>
            ))}
          </nav>

          <div className="settings-content">
            <SettingsCard id="paths" title="Library folders" subtitle="Folders scanned recursively for FITS and XISF files">
              <div className="form-row">
                <button className="btn-secondary" onClick={openPicker}><Icon name="folder" size={15} />Browse…</button>
                <input type="text" placeholder="…or type a folder path" value={newPath} aria-label="Folder path"
                       onChange={e => setNewPath(e.target.value)}
                       onKeyDown={e => e.key === 'Enter' && handleAddPath()} />
                <button className="btn-primary" onClick={() => handleAddPath()} disabled={!newPath.trim()}>
                  <Icon name="plus" size={14} />Add
                </button>
              </div>
              {addError && <Alert>{addError}</Alert>}
              {!pathsLoaded ? (
                <LoadingBlock>Loading folders…</LoadingBlock>
              ) : paths.length === 0 ? (
                <div className="list-empty">No folders yet. Use Browse… to pick the folders that hold your frames.</div>
              ) : (
                <div className="list-frame">
                  {paths.map(p => (
                    <div key={p.id} className="list-row">
                      <Icon name="folder" size={15} />
                      <span className="path-item-text" title={p.path}>{p.path}</span>
                      <button className="btn-ghost btn-sm" onClick={() => runJob(() => triggerScanPath(p.id), 'Scan')}
                              title="Scan only this folder for new or changed files">
                        <Icon name="refresh" size={13} />Scan
                      </button>
                      <button className="icon-btn danger" onClick={() => handleDeletePath(p)}
                              title="Remove folder" aria-label={`Remove ${p.path}`}>
                        <Icon name="trash" size={15} />
                      </button>
                    </div>
                  ))}
                </div>
              )}
            </SettingsCard>

            <SettingsCard id="jobs" title="Jobs & queue" subtitle="Scanning, analysis and solving run on the worker queue">
              <div className="stack-sm">
                <div className="group-label">Library</div>
                <div className="row">
                  <button className="btn-primary" onClick={() => runJob(triggerScan, 'Scan')}>
                    <Icon name="refresh" size={14} />Scan all folders
                  </button>
                  <button className="btn-secondary" onClick={() => runJob(() => triggerReanalyze(true), 'Re-analyze')}>
                    Re-analyze missing
                  </button>
                  <button className="btn-secondary" onClick={reanalyzeAll}>Re-analyze all…</button>
                </div>
              </div>
              <div className="stack-sm">
                <div className="group-label">Plate solving</div>
                <div className="row">
                  <button className="btn-secondary" onClick={() => runJob(() => triggerPlatesolve(true), 'Plate solve')}
                          disabled={!astap.available} title={astap.available ? '' : 'Set up ASTAP first'}>
                    <Icon name="crosshair" size={14} />Solve unsolved
                  </button>
                  <button className="btn-secondary" onClick={resolveAll} disabled={!astap.available}
                          title={astap.available ? '' : 'Set up ASTAP first'}>
                    Re-solve all (verify)…
                  </button>
                </div>
              </div>
              <QueuePanel />
              <p className="help-text">
                Jobs are processed by worker containers — scale with <code>docker compose up --scale worker=N</code>.
                Re-analyze backfills HFR, FWHM, sky geometry and WCS. Plate solving needs ASTAP and a star database.
              </p>
            </SettingsCard>

            <SettingsCard id="site" title="Observing site"
                          subtitle="Used for altitude, airmass and moon geometry when a frame's header has no site coordinates"
                          footer={<button className="btn-primary" onClick={handleSaveSite}>Save site</button>}>
              <div className="form-grid">
                <div className="field">
                  <label htmlFor="site-lat">Latitude (°, north +)</label>
                  <input id="site-lat" type="number" step="any" placeholder="e.g. 37.5" value={site.lat}
                         onChange={e => setSiteState({ ...site, lat: e.target.value })} />
                </div>
                <div className="field">
                  <label htmlFor="site-lon">Longitude (°, east +)</label>
                  <input id="site-lon" type="number" step="any" placeholder="e.g. -122.0" value={site.lon}
                         onChange={e => setSiteState({ ...site, lon: e.target.value })} />
                </div>
                <div className="field">
                  <label htmlFor="site-elev">Elevation (m)</label>
                  <input id="site-elev" type="number" step="any" placeholder="e.g. 100" value={site.elev}
                         onChange={e => setSiteState({ ...site, elev: e.target.value })} />
                </div>
              </div>
            </SettingsCard>

            <SettingsCard id="solving" title="Plate solving (ASTAP)"
                          subtitle="Solve light frames without a WCS. Frames already solved by your capture software are read automatically.">
              <div className="field">
                <label htmlFor="astap-path">ASTAP executable</label>
                <div className="form-row">
                  <input id="astap-path" type="text" placeholder="e.g. /usr/local/bin/astap" value={astap.path}
                         onChange={e => setAstapState({ ...astap, path: e.target.value })}
                         onKeyDown={e => e.key === 'Enter' && handleSaveAstap()} />
                  <button className="btn-primary" onClick={handleSaveAstap}>Save</button>
                </div>
              </div>
              {astap.available
                ? <span className="status-text ok"><Icon name="check" size={14} />ASTAP is ready</span>
                : <span className="status-text"><Icon name="info" size={14} />Not ready — set the executable path and install a star database below</span>}

              <div>
                <div className="sub-heading">Star databases</div>
                {dbs ? (
                  <div className="list-frame">
                    {dbs.databases.map(d => (
                      <div key={d.key} className="list-row">
                        <Icon name="database" size={15} />
                        <div className="db-info">
                          <span className="db-name">{d.name}</span>
                          <span className="db-note">{d.note} · ~{d.size_mb} MB</span>
                        </div>
                        {d.installed
                          ? <span className="badge badge-success"><Icon name="check" size={11} strokeWidth={2.5} />Installed</span>
                          : (
                            <button className="btn-secondary btn-sm" disabled={!!dbDownloading} onClick={() => handleDownloadDb(d.key)}>
                              {dbDownloading === d.key
                                ? <><Spinner size={12} />Downloading…</>
                                : <><Icon name="download" size={13} />Download</>}
                            </button>
                          )}
                      </div>
                    ))}
                  </div>
                ) : <LoadingBlock>Loading databases…</LoadingBlock>}
              </div>
              {dbProgress?.phase === 'download' && (
                <div className="stack-sm">
                  <div className="progress"><div className="progress-fill" style={{ width: `${dlPct ?? 0}%` }} /></div>
                  <span className="help-text">
                    Downloading {dbProgress.mb}{dbProgress.total_mb ? ` of ${dbProgress.total_mb}` : ''} MB
                    {dlPct != null ? ` (${dlPct}%)` : ''}
                  </span>
                </div>
              )}
              {dbProgress?.phase === 'extract' && <span className="status-text"><Spinner size={13} />Extracting…</span>}
              {dbProgress?.phase === 'error' && <Alert>Download failed: {dbProgress.message}</Alert>}
            </SettingsCard>

            <SettingsCard id="export" title="Export path mapping"
                          subtitle="Rewrite container paths to the paths PixInsight sees in exported LIGHT lists"
                          footer={<button className="btn-primary" onClick={handleSaveExportMap}>Save mapping</button>}>
              <div className="form-grid form-grid-wide">
                <div className="field">
                  <label htmlFor="map-src">Container prefix (from)</label>
                  <input id="map-src" type="text" placeholder="/mnt/astro" value={exportMap.src}
                         onChange={e => setExportMapState({ ...exportMap, src: e.target.value })} />
                </div>
                <div className="field">
                  <label htmlFor="map-dst">PixInsight prefix (to)</label>
                  <input id="map-dst" type="text" placeholder="P:\astro" value={exportMap.dst}
                         onChange={e => setExportMapState({ ...exportMap, dst: e.target.value })} />
                </div>
              </div>
            </SettingsCard>

            <BenchmarkCard id={sectionId('benchmark')} />

            <SettingsCard id="maintenance" title="Maintenance" subtitle="Clean up catalog entries for files that are no longer on disk">
              <div className="row">
                <button className="btn-secondary" onClick={handlePrune} disabled={pruning}>
                  {pruning ? <><Spinner size={13} />Pruning…</> : <><Icon name="wrench" size={14} />Prune missing files…</>}
                </button>
              </div>
            </SettingsCard>

            <SettingsCard id="danger" danger title="Danger zone" subtitle="Wipe all watched folders, indexed files and thumbnails. This cannot be undone.">
              <div className="field">
                <label htmlFor="reset-confirm">Type <code>{RESET_PHRASE}</code> to enable the reset</label>
                <div className="reset-row">
                  <input id="reset-confirm" type="text" placeholder={RESET_PHRASE} value={resetConfirm} autoComplete="off"
                         onChange={e => setResetConfirm(e.target.value)}
                         onKeyDown={e => e.key === 'Enter' && resetConfirm === RESET_PHRASE && handleReset()} />
                  <button className="btn-danger-solid" onClick={handleReset} disabled={resetConfirm !== RESET_PHRASE || resetting}>
                    <Icon name="trash" size={14} />{resetting ? 'Resetting…' : 'Reset database'}
                  </button>
                </div>
              </div>
            </SettingsCard>

            <SettingsCard id="about" title="About">
              <dl className="about-list">
                <dt>Application</dt><dd>fitsql</dd>
                <dt>Formats</dt><dd>FITS (.fit, .fits, .fts) and XISF (.xisf)</dd>
                <dt>Backend</dt><dd>FastAPI · SQLAlchemy · astropy · SEP · Redis queue with workers</dd>
                <dt>Frontend</dt><dd>React · Vite</dd>
                <dt>Public API</dt>
                <dd><a href={`${BASE}/api/v1/docs`} target="_blank" rel="noreferrer">/api/v1/docs <Icon name="external" size={12} /></a></dd>
              </dl>
            </SettingsCard>
          </div>
        </div>
      </div>

      {pickerOpen && (
        <Modal title="Choose a folder to watch" subtitle="The folder and all its subfolders are scanned"
               onClose={() => setPickerOpen(false)}
               footer={<>
                 <button className="btn-secondary" onClick={() => setPickerOpen(false)}>Cancel</button>
                 <button className="btn-primary" disabled={!pickerData?.path} onClick={addFromPicker} data-autofocus>
                   Add this folder
                 </button>
               </>}>
          <div className="picker-path">{pickerData?.path || '/'}</div>
          <div className="picker-toolbar">
            <button className="btn-secondary btn-sm" disabled={!pickerData?.parent && !pickerData?.path}
                    onClick={() => navigate(pickerData?.parent || null)}>
              <Icon name="arrowUp" size={13} />Up
            </button>
            {pickerData?.has_fits && <span className="badge badge-success"><Icon name="check" size={11} />Contains frames</span>}
          </div>
          {pickerError && <Alert>{pickerError}</Alert>}
          <div className="picker-list">
            {pickerLoading ? (
              <div className="picker-empty"><Spinner size={14} />Loading…</div>
            ) : pickerData && pickerData.dirs.length === 0 ? (
              <div className="picker-empty">No subfolders here.</div>
            ) : (
              pickerData?.dirs.map(d => (
                <button key={d.path} className="picker-dir" onClick={() => navigate(d.path)}>
                  <Icon name="folder" size={15} />{d.name}
                </button>
              ))
            )}
          </div>
        </Modal>
      )}
    </div>
  )
}
