import { useState, useEffect, useCallback } from 'react'
import { runQuery, exportList, exportXpsm, solveSelection, getStats, errorText } from '../components/api.js'
import FileCard from '../components/FileCard.jsx'
import FileDetail from '../components/FileDetail.jsx'
import SavedQueries from '../components/SavedQueries.jsx'
import useGrading from '../components/useGrading.js'
import CompareView, { CompareTray, useCompare } from '../components/CompareView.jsx'
import Icon from '../components/Icon.jsx'
import { Alert, EmptyState, PageHeader, Pagination, Spinner, Stat, useConfirm, useToast } from '../components/ui.jsx'
import { fmtHours } from '../components/format.js'

const PER_PAGE = 50
const NO_ITEMS = []

// UI builder fields -> expression clause generators
const BUILDER = [
  { key: 'hfr_max', label: 'Max HFR (px)', clause: v => `HFR < ${v}` },
  { key: 'fwhm_max', label: 'Max FWHM (px)', clause: v => `FWHM < ${v}` },
  { key: 'ecc_max', label: 'Max eccentricity', clause: v => `Eccentricity < ${v}` },
  { key: 'stars_min', label: 'Min stars', clause: v => `Stars > ${v}` },
  { key: 'alt_min', label: 'Min altitude (°)', clause: v => `Altitude > ${v}` },
  { key: 'airmass_max', label: 'Max airmass', clause: v => `Airmass < ${v}` },
  { key: 'moonsep_min', label: 'Min moon separation (°)', clause: v => `MoonSep > ${v}` },
  { key: 'moonillum_max', label: 'Max moon illumination', clause: v => `MoonIllum < ${v}` },
  { key: 'pixscale_max', label: 'Max pixel scale (″/px)', clause: v => `pixel_scale < ${v}` },
  { key: 'focal_min', label: 'Min focal length (mm)', clause: v => `Focal > ${v}` },
  { key: 'focal_max', label: 'Max focal length (mm)', clause: v => `Focal < ${v}` },
  { key: 'fov_min', label: 'Min field of view (°)', clause: v => `fov > ${v}` },
]

const PRESET_GROUPS = [
  {
    label: 'Statistical outliers',
    items: [
      { label: 'HFR outliers', clause: 'HFR < median(HFR) + 2*mad(HFR)' },
      { label: 'FWHM outliers', clause: 'FWHM < median(FWHM) + 2*mad(FWHM)' },
      { label: 'Eccentricity outliers', clause: 'Eccentricity < median(Eccentricity) + 2*mad(Eccentricity)' },
      { label: 'PSF FWHM outliers', clause: 'psf_fwhm < median(psf_fwhm) + 2*mad(psf_fwhm)' },
      { label: 'Top 75% by quality', clause: 'Quality > percentile(Quality, 25)' },
    ],
  },
  {
    label: 'Sky & tracking',
    items: [
      { label: 'Good tracking only', clause: 'good_tracking' },
      { label: 'No occlusion', clause: '!occluded' },
      { label: 'No satellite streaks', clause: '!satellite' },
      { label: 'No frost / dew shadow', clause: '!shadow' },
      { label: 'Transparency ≥ 0.8', clause: 'transparency >= 0.8' },
      { label: 'Steady session (no clouds)', clause: '!seq_anomaly' },
    ],
  },
  {
    label: 'Plate solving',
    items: [
      { label: 'Plate-solved only', clause: 'solved == 1' },
      { label: 'Needs solving', clause: 'missing(astap_ra)' },
      { label: 'No coordinates', clause: 'isnan(ra)' },
      { label: 'Coord mismatch (>10′)', clause: 'coord_sep > 10' },
    ],
  },
  {
    label: 'Grades, paths & sensor',
    items: [
      { label: 'Exclude rejected', clause: '!rejected' },
      { label: 'Accepted only', clause: 'accepted' },
      { label: 'Exclude reject folders', clause: "path not like '%reject%'" },
      { label: 'OSC only', clause: 'osc' },
      { label: 'Mono only', clause: 'mono' },
      { label: 'Sensor at setpoint', clause: '!off_setpoint' },
    ],
  },
]

const REFERENCE = [
  { label: 'Star metrics', items: ['HFR', 'FWHM', 'FWHM_arcsec', 'psf_fwhm', 'psf_fwhm_arcsec', 'psf_beta', 'Eccentricity', 'trail_score', 'Stars', 'Quality', 'star_flux', 'streaks'] },
  { label: 'Background & grid', items: ['Background', 'bg_spread', 'bg_dip', 'empty_cells'] },
  { label: 'Versus own session', items: ['star_drop', 'hfr_rise', 'empty_rise', 'spread_rise', 'flux_drop', 'dip_rise', 'transparency'] },
  { label: 'Capture & optics', items: ['Exptime', 'Focal', 'pixel_scale', 'width', 'height', 'fov', 'fov_w', 'fov_h', 'ccd_temp', 'set_temp', 'temp_delta'] },
  { label: 'Pointing & plate solve', items: ['RA', 'Dec', 'acquisition_ra/dec', 'astap_ra/dec', 'coord_sep', 'solved', 'wcs_scale', 'wcs_rotation'] },
  { label: 'Sky geometry', items: ['Altitude', 'Airmass', 'MoonSep', 'MoonIllum', 'MoonAlt'] },
  { label: 'Text fields', items: ["filter=='h'", 'object', 'imagetyp', 'telescop', 'instrume', 'bayer', 'path', 'filename'] },
  { label: 'Time (UTC)', items: ["date == '2026-05-13'", 'hour'] },
  { label: 'Flags', items: ['light / dark / flat / bias', 'osc / mono', 'rejected / accepted', 'good_tracking / trailed', 'occluded', 'satellite', 'shadow', 'off_setpoint', 'seq_anomaly / seq_ok'] },
  { label: 'Functions', items: ['median', 'mean', 'std', 'mad', 'madstd', 'percentile(x, q)', 'min', 'max', 'abs', 'sqrt', 'isnan(x)', 'missing(x)', 'present(x)', 'contains(ra, dec)', 'angsep(ra, dec)', 'like(x, pat)', 'between(x, lo, hi)'] },
  { label: 'Operators', items: ['&&', '||', '!', '<', '>', '+  -  *  /', 'hfr not between 2 and 4', "date between '2026-05-01' and '2026-05-31'"] },
  { label: 'Patterns (% any run, _ one char)', items: ["path not like '%reject%'", "filename like 'M31_%'"] },
]

const REF_MODES = [
  ['max_fov', 'Reference: largest FOV'],
  ['min_fov', 'Reference: smallest FOV'],
  ['max_scale', 'Reference: highest px scale'],
  ['min_scale', 'Reference: lowest px scale'],
]

function Facet({ title, items, onPick }) {
  if (!items || items.length === 0) return null
  return (
    <div className="facet">
      <div className="facet-title">{title}</div>
      <div className="facet-list">
        {items.map(it => (
          <button key={it.value} className="facet-item" onClick={() => onPick(it.value)} title={`Filter to ${it.value}`}>
            <span className="facet-val">{it.value}</span>
            <span className="facet-count">{it.count.toLocaleString()}</span>
          </button>
        ))}
      </div>
    </div>
  )
}

export default function SelectorPage() {
  const [stats, setStats] = useState(null)
  const [base, setBase] = useState({ imagetyp: 'Light Frame', object: '', filter: '' })
  const [expression, setExpression] = useState('HFR < median(HFR) + 2*mad(HFR) && Eccentricity < 0.6')
  const [builder, setBuilder] = useState({})
  const [target, setTarget] = useState({ ra: '', dec: '' })
  const [refMode, setRefMode] = useState('max_fov')
  const [result, setResult] = useState(null)
  const [page, setPage] = useState(1)
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState(null)
  const [exporting, setExporting] = useState(false)
  const setItems = useCallback(fn => setResult(r => (r ? { ...r, items: fn(r.items) } : r)), [])
  const grading = useGrading(result?.items ?? NO_ITEMS, setItems)
  const compare = useCompare()
  const toast = useToast()
  const confirm = useConfirm()

  useEffect(() => { getStats().then(setStats).catch(() => {}) }, [])

  const body = useCallback((pg) => ({
    expression,
    imagetyp: base.imagetyp || null,
    object: base.object || null,
    filter: base.filter || null,
    telescop: base.telescop || null,
    instrume: base.instrume || null,
    date_from: base.date_from || null,
    date_to: base.date_to || null,
    page: pg,
    per_page: PER_PAGE,
  }), [expression, base])

  const doQuery = useCallback(async (pg = 1) => {
    setLoading(true)
    setError(null)
    try {
      const r = await runQuery(body(pg))
      setResult(r)
      setPage(pg)
    } catch (e) {
      setError(e.message)
      setResult(null)
    } finally {
      setLoading(false)
    }
  }, [body])

  // Run immediately against a given base (+ optional explicit expression) —
  // avoids the stale-closure problem on facet clicks and saved-query loads.
  const runWithBase = useCallback(async (b, expr = expression) => {
    setLoading(true); setError(null)
    try {
      const r = await runQuery({
        expression: expr, page: 1, per_page: PER_PAGE,
        imagetyp: b.imagetyp || null, object: b.object || null, filter: b.filter || null,
        telescop: b.telescop || null, instrume: b.instrume || null,
        date_from: b.date_from || null, date_to: b.date_to || null,
      })
      setResult(r); setPage(1)
    } catch (e) { setError(e.message); setResult(null) } finally { setLoading(false) }
  }, [expression])

  const applyFacet = (key, value) => {
    const upd = key === 'date' ? { date_from: value, date_to: value } : { [key]: String(value) }
    const nb = { ...base, ...upd }
    setBase(nb); runWithBase(nb)
  }
  const clearFilters = (keys) => {
    const nb = { ...base }; keys.forEach(k => { nb[k] = '' })
    setBase(nb); runWithBase(nb)
  }

  // Saved queries: capture/restore the full Selector state
  const currentState = () => ({ expression, ...base, ref_mode: refMode })
  const loadSaved = (q) => {
    const s = q.query || {}
    const nb = {
      imagetyp: s.imagetyp ?? 'Light Frame', object: s.object ?? '', filter: s.filter ?? '',
      telescop: s.telescop ?? '', instrume: s.instrume ?? '',
      date_from: s.date_from ?? '', date_to: s.date_to ?? '',
    }
    setExpression(s.expression || '')
    setBase(nb)
    if (s.ref_mode) setRefMode(s.ref_mode)
    runWithBase(nb, s.expression || '')
    toast(`Loaded “${q.name}”`, { tone: 'success' })
  }

  const appendClause = (clause) => {
    setExpression(e => (e.trim() ? `${e.trim()} && ${clause}` : clause))
  }

  const buildFromFields = () => {
    const clauses = BUILDER
      .filter(b => builder[b.key] !== undefined && builder[b.key] !== '')
      .map(b => b.clause(builder[b.key]))
    if (clauses.length) setExpression(clauses.join(' && '))
  }

  const handleExport = async () => {
    setExporting(true)
    try {
      await exportList({ ...body(1), per_page: 1 })
    } catch (e) {
      toast(`Export failed: ${errorText(e)}`, { tone: 'danger' })
    } finally {
      setExporting(false)
    }
  }

  const handleExportXpsm = async () => {
    setExporting(true)
    try {
      await exportXpsm({ ...body(1), per_page: 1 }, refMode)
    } catch (e) {
      toast(`Export failed: ${errorText(e)}`, { tone: 'danger' })
    } finally {
      setExporting(false)
    }
  }

  const handleSolveSelection = async () => {
    if (!result) return
    const ok = await confirm({
      title: 'Plate-solve these frames?',
      message: `ASTAP will solve all ${result.approved.toLocaleString()} approved frames on the worker queue. Originals are never modified.`,
      confirmLabel: 'Queue solves',
    })
    if (!ok) return
    try {
      const r = await solveSelection({ ...body(1), per_page: 1 })
      toast(`Queued ${r.queued.toLocaleString()} plate solves`, { tone: 'success' })
    } catch (e) {
      toast(`Couldn't queue solves: ${errorText(e)}`, { tone: 'danger' })
    }
  }

  const noneApproved = !result || result.approved === 0
  const activeFilters = [['object', 'Object'], ['filter', 'Filter'], ['telescop', 'Telescope'], ['instrume', 'Camera']]
    .filter(([k]) => base[k])

  return (
    <div className="page">
      <PageHeader title="Frame Selector"
                  subtitle="Approve frames with SubframeSelector-style expressions, then export them for WBPP" />

      <div className="page-body">
        <div className="stack constrain">
          <SavedQueries currentState={currentState} onLoad={loadSaved} />

          <section className="card">
            <div className="card-header">
              <div>
                <h2 className="card-title">Query</h2>
                <p className="card-subtitle">Scope the frames, then write the expression that approves them</p>
              </div>
            </div>
            <div className="card-body">
              <div className="scope-row">
                <div className="field">
                  <label htmlFor="sel-type">Image type</label>
                  <select id="sel-type" value={base.imagetyp} onChange={e => setBase({ ...base, imagetyp: e.target.value })}>
                    <option value="Light Frame">Light Frame</option>
                    {(stats?.distinct?.imagetypes || []).filter(t => t !== 'Light Frame').map(t => (
                      <option key={t} value={t}>{t}</option>
                    ))}
                  </select>
                </div>
                <div className="field">
                  <label htmlFor="sel-object">Object</label>
                  <input id="sel-object" type="text" placeholder="All objects" value={base.object}
                         onChange={e => setBase({ ...base, object: e.target.value })}
                         onKeyDown={e => { if (e.key === 'Enter') doQuery(1) }} />
                </div>
                <div className="field">
                  <label htmlFor="sel-filter">Filter</label>
                  <select id="sel-filter" value={base.filter} onChange={e => setBase({ ...base, filter: e.target.value })}>
                    <option value="">All filters</option>
                    {(stats?.distinct?.filters || []).map(f => <option key={f} value={f}>{f}</option>)}
                  </select>
                </div>
              </div>

              <div className="expr-editor">
                <div className="expr-editor-head">
                  <label htmlFor="sel-expr">Approval expression</label>
                  <span className="expr-hint"><kbd>Ctrl</kbd><kbd>Enter</kbd> to run</span>
                </div>
                <textarea
                  id="sel-expr"
                  value={expression}
                  onChange={e => setExpression(e.target.value)}
                  onKeyDown={e => { if (e.key === 'Enter' && (e.ctrlKey || e.metaKey)) { e.preventDefault(); doQuery(1) } }}
                  spellCheck={false}
                  rows={2}
                  placeholder="e.g. HFR < 3.5 && Eccentricity < 0.6 && FWHM < median(FWHM) + 2*mad(FWHM)"
                />
              </div>

              <div className="preset-groups">
                {PRESET_GROUPS.map(g => (
                  <div key={g.label} className="preset-group">
                    <div className="group-label">{g.label}</div>
                    <div className="chips">
                      {g.items.map(p => (
                        <button key={p.label} className="chip-btn" onClick={() => appendClause(p.clause)}
                                title={`Append: ${p.clause}`}>
                          <Icon name="plus" size={12} />{p.label}
                        </button>
                      ))}
                    </div>
                  </div>
                ))}
              </div>

              <div className="target-helper">
                <span>Frame contains target</span>
                <input type="number" step="any" placeholder="RA °" aria-label="Target RA in degrees" value={target.ra}
                       onChange={e => setTarget({ ...target, ra: e.target.value })} />
                <input type="number" step="any" placeholder="Dec °" aria-label="Target Dec in degrees" value={target.dec}
                       onChange={e => setTarget({ ...target, dec: e.target.value })} />
                <button className="chip-btn" disabled={target.ra === '' || target.dec === ''}
                        onClick={() => appendClause(`contains(${target.ra}, ${target.dec})`)}
                        title="Frame footprint covers the position">
                  <Icon name="plus" size={12} />contains()
                </button>
                <button className="chip-btn" disabled={target.ra === '' || target.dec === ''}
                        onClick={() => appendClause(`angsep(${target.ra}, ${target.dec}) < 1`)}
                        title="Frame center within 1° of target">
                  <Icon name="plus" size={12} />angsep() &lt; 1°
                </button>
                <span className="spacer" />
                <button className="btn-ghost btn-sm" onClick={() => setExpression('')} disabled={!expression}>
                  <Icon name="x" size={13} />Clear expression
                </button>
              </div>

              <div className="selector-extras">
                <details className="disclosure">
                  <summary><Icon name="chevronDown" size={14} />Min / max builder</summary>
                  <div className="disclosure-body">
                    <div className="builder-grid">
                      {BUILDER.map(b => (
                        <div key={b.key} className="field">
                          <label htmlFor={`b-${b.key}`}>{b.label}</label>
                          <input id={`b-${b.key}`} type="number" step="any" placeholder="—"
                                 value={builder[b.key] ?? ''}
                                 onChange={e => setBuilder({ ...builder, [b.key]: e.target.value })} />
                        </div>
                      ))}
                    </div>
                    <button className="btn-secondary btn-sm" onClick={buildFromFields}>
                      Replace expression with these limits<Icon name="chevronRight" size={13} />
                    </button>
                  </div>
                </details>
                <details className="disclosure">
                  <summary><Icon name="chevronDown" size={14} />Expression reference</summary>
                  <div className="disclosure-body">
                    <div className="ref-grid">
                      {REFERENCE.map(g => (
                        <div key={g.label} className="ref-group">
                          <div className="group-label">{g.label}</div>
                          <div className="ref-items">{g.items.map(it => <code key={it}>{it}</code>)}</div>
                        </div>
                      ))}
                    </div>
                  </div>
                </details>
              </div>
            </div>

            <div className="card-footer query-actions">
              <button className="btn-primary" onClick={() => doQuery(1)} disabled={loading}>
                {loading ? <Spinner size={14} /> : <Icon name="play" size={14} />}
                {loading ? 'Running…' : 'Run query'}
              </button>
              <span className="spacer" />
              <button className="btn-secondary" onClick={handleExport} disabled={exporting || noneApproved}
                      title="Plain text list of approved LIGHT paths">
                <Icon name="list" size={15} />Export list{result ? ` (${result.approved.toLocaleString()})` : ''}
              </button>
              <div className="btn-group">
                <select value={refMode} onChange={e => setRefMode(e.target.value)} aria-label="Reference frame selection">
                  {REF_MODES.map(([v, label]) => <option key={v} value={v}>{label}</option>)}
                </select>
                <button className="btn-secondary" onClick={handleExportXpsm} disabled={exporting || noneApproved}
                        title="PixInsight FastIntegration process icon">
                  <Icon name="download" size={15} />FastIntegration .xpsm
                </button>
              </div>
              <button className="btn-secondary" onClick={handleSolveSelection} disabled={noneApproved}
                      title="ASTAP-solve every approved frame">
                <Icon name="crosshair" size={15} />Solve{result ? ` (${result.approved.toLocaleString()})` : ''}
              </button>
            </div>
          </section>

          {error && <Alert>{errorText(error)}</Alert>}

          {(activeFilters.length > 0 || base.date_from) && (
            <div className="active-filters">
              <span className="af-label">Narrowed to</span>
              {activeFilters.map(([k, label]) => (
                <span key={k} className="af-chip">
                  <small>{label}</small><span>{base[k]}</span>
                  <button onClick={() => clearFilters([k])} aria-label={`Remove ${label} filter`}><Icon name="x" size={12} /></button>
                </span>
              ))}
              {base.date_from && (
                <span className="af-chip">
                  <small>Night</small><span>{base.date_from}</span>
                  <button onClick={() => clearFilters(['date_from', 'date_to'])} aria-label="Remove date filter"><Icon name="x" size={12} /></button>
                </span>
              )}
            </div>
          )}

          {!result && !loading && !error && (
            <EmptyState icon="sliders" title="Run a query to screen frames">
              Pick presets or write an expression, then run it to see which frames are approved.
            </EmptyState>
          )}

          {result && (
            <section className="card">
              <div className="card-header">
                <div>
                  <h2 className="card-title">Results</h2>
                  <p className="card-subtitle">Click a breakdown value to narrow the scope</p>
                </div>
                {loading && <Spinner />}
              </div>
              <div className="card-body">
                <div className="stats-row">
                  <Stat label="Approved" value={result.approved.toLocaleString()} tone="success"
                        sub={`of ${result.total.toLocaleString()} frames`} />
                  <Stat label="Rejected" value={result.rejected.toLocaleString()} tone={result.rejected ? 'danger' : undefined} />
                  <Stat label="Kept" value={result.total > 0 ? `${Math.round(result.approved / result.total * 100)}%` : '—'} />
                  {result.facets && (
                    <Stat label="Approved integration" value={fmtHours(result.facets.integration_sec)} tone="accent" />
                  )}
                </div>

                {result.facets && (
                  <div className="facets">
                    <Facet title="Object" items={result.facets.object} onPick={v => applyFacet('object', v)} />
                    <Facet title="Filter" items={result.facets.filter} onPick={v => applyFacet('filter', v)} />
                    <Facet title="Camera" items={result.facets.instrume} onPick={v => applyFacet('instrume', v)} />
                    <Facet title="Telescope" items={result.facets.telescop} onPick={v => applyFacet('telescop', v)} />
                    <Facet title="Night" items={result.facets.date} onPick={v => applyFacet('date', v)} />
                  </div>
                )}

                {result.stats && Object.keys(result.stats).length > 0 && (
                  <div className="table-frame">
                    <table className="data-table compact">
                      <thead>
                        <tr>
                          <th>Metric</th><th className="num">Median</th><th className="num">MAD</th>
                          <th className="num">Mean</th><th className="num">Std</th>
                          <th className="num">Min</th><th className="num">Max</th>
                        </tr>
                      </thead>
                      <tbody>
                        {Object.entries(result.stats).map(([k, s]) => (
                          <tr key={k}>
                            <td className="metric-name">{k}</td>
                            <td className="num">{s.median}</td><td className="num">{s.mad}</td>
                            <td className="num">{s.mean}</td><td className="num">{s.std}</td>
                            <td className="num">{s.min}</td><td className="num">{s.max}</td>
                          </tr>
                        ))}
                      </tbody>
                    </table>
                  </div>
                )}
              </div>
            </section>
          )}

          {result && (result.items.length > 0 ? (
            <div className="results-grid">
              <div className={`file-grid${loading ? ' is-stale' : ''}`}>
                {result.items.map(f => (
                  <FileCard key={f.id} file={f} onClick={grading.open}
                            onCompare={compare.toggle} inCompare={compare.has(f.id)} />
                ))}
              </div>
              <Pagination page={page} pages={result.pages} total={result.approved} perPage={PER_PAGE}
                          onPage={p => doQuery(p)} disabled={loading} />
            </div>
          ) : (
            <EmptyState icon="search" title="No frames approved">
              {result.total > 0
                ? `All ${result.total.toLocaleString()} frames in scope were rejected — loosen the expression or check that the metrics it uses have been measured.`
                : 'Nothing is in scope — widen the image type, object or filter.'}
            </EmptyState>
          ))}
        </div>
      </div>

      {grading.current && (
        <FileDetail file={grading.current} grading={grading} compare={compare} onClose={grading.close} />
      )}
      <CompareTray compare={compare} onOpen={() => { grading.close(); compare.open() }} />
      {compare.isOpen && <CompareView files={compare.files} onClose={compare.close} onRemove={compare.toggle} />}
    </div>
  )
}
