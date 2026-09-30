import { useState, useEffect, useCallback } from 'react'
import {
  getTargets, getTarget,
  getUserTargets, createUserTarget, deleteUserTarget, getUserTargetDetail,
  createTargetFromObject, errorText,
} from '../components/api.js'
import Icon from '../components/Icon.jsx'
import { Alert, EmptyState, FilterChip, LoadingBlock, Modal, PageHeader, useConfirm, useToast } from '../components/ui.jsx'
import { filterColor, fmtHours, plural, qualityColor } from '../components/format.js'

function FilterBar({ filters, total }) {
  if (!total) return null
  return (
    <span className="tg-bar" style={{ display: 'flex' }}
          title={filters.map(f => `${f.filter}: ${fmtHours(f.integration_sec)}`).join(' · ')}>
      {filters.map(f => (
        <span key={f.filter} className="tg-bar-seg"
              style={{ flex: `${f.integration_sec} 1 0`, background: filterColor(f.filter) }} />
      ))}
    </span>
  )
}

function NightTable({ nights }) {
  return (
    <div className="table-frame">
      <table className="data-table compact">
        <thead>
          <tr>
            <th>Night</th><th>Filter</th><th className="num">Frames</th><th className="num">Integration</th>
            <th className="num">HFR</th><th className="num">Ecc</th><th className="num">Altitude</th><th className="num">Quality</th>
          </tr>
        </thead>
        <tbody>
          {nights.map(n => n.filters.map((f, i) => (
            <tr key={`${n.night}-${f.filter}`}>
              <td>{i === 0 ? <span className="tg-night">{n.night}</span> : ''}</td>
              <td><FilterChip name={f.filter} /></td>
              <td className="num">{f.frames}</td>
              <td className="num">{fmtHours(f.integration_sec)}</td>
              <td className="num">{f.avg_hfr ?? '—'}</td>
              <td className="num">{f.avg_eccentricity ?? '—'}</td>
              <td className="num">{f.avg_altitude != null ? `${f.avg_altitude}°` : '—'}</td>
              <td className="num" style={{ color: qualityColor(f.avg_quality), fontWeight: 600 }}>{f.avg_quality ?? '—'}</td>
            </tr>
          )))}
        </tbody>
      </table>
    </div>
  )
}

// Shared card: rollup stats + filter bar, expanding to a per-night table
function TargetCard({ t, subtitle, onToggle, isOpen, detail, actions }) {
  const range = t.first_night && (t.first_night === t.last_night ? t.first_night : `${t.first_night} → ${t.last_night}`)
  return (
    <div className={`tg-card${isOpen ? ' open' : ''}`}>
      <div className="tg-head">
        <button className="tg-toggle" onClick={onToggle} aria-expanded={isOpen}>
          <span className="tg-row">
            <Icon name="chevronDown" size={15} className="tg-chevron" />
            <span className="tg-name">{t.name || t.object}</span>
            <span className="tg-int">{fmtHours(t.integration_sec)}</span>
            <span className="tg-meta">{plural(t.frames, 'frame')} · {plural(t.nights, 'night')}</span>
            {range && <span className="tg-meta">{range}</span>}
            {t.avg_hfr != null && <span className="tg-meta">HFR {t.avg_hfr}</span>}
            {t.avg_quality != null && (
              <span className="badge" style={{ color: qualityColor(t.avg_quality) }}>Q {t.avg_quality}</span>
            )}
            {t.rejected > 0 && <span className="badge badge-danger">{t.rejected} rejected</span>}
          </span>
          {subtitle}
          <FilterBar filters={t.filters || []} total={t.integration_sec} />
          <span className="tg-filter-totals">
            {(t.filters || []).map(f => (
              <span key={f.filter} className="tg-chip">
                <i style={{ background: filterColor(f.filter) }} /><b>{f.filter}</b>{fmtHours(f.integration_sec)}
              </span>
            ))}
            {(t.rigs || []).slice(0, 2).map((r, i) => (
              <span key={i} className="tg-rig">{[r.telescop, r.instrume].filter(Boolean).join(' · ')}</span>
            ))}
          </span>
        </button>
        {actions && <div className="tg-actions">{actions}</div>}
      </div>

      {isOpen && (
        <div className="tg-detail">
          {detail?.error ? <Alert>Couldn't load nights: {detail.error}</Alert>
            : !detail ? <LoadingBlock>Loading nights…</LoadingBlock> : (
              <div className="stack-sm">
                <div className="tg-filter-totals">
                  {detail.filters.map(f => (
                    <span key={f.filter} className="tg-chip">
                      <i style={{ background: filterColor(f.filter) }} /><b>{f.filter}</b>
                      {fmtHours(f.integration_sec)} <span className="text-muted">· {plural(f.frames, 'frame')}</span>
                    </span>
                  ))}
                </div>
                <NightTable nights={detail.nights} />
              </div>
            )}
        </div>
      )}
    </div>
  )
}

function AddTargetDialog({ onClose, onCreated }) {
  const [form, setForm] = useState({ name: '', ra: '', dec: '', radius: '' })
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState(null)
  const set = (k) => (e) => setForm({ ...form, [k]: e.target.value })

  const ra = parseFloat(form.ra)
  const dec = parseFloat(form.dec)
  const radius = form.radius.trim() ? parseFloat(form.radius) : null
  const raOk = Number.isFinite(ra) && ra >= 0 && ra < 360
  const decOk = Number.isFinite(dec) && dec >= -90 && dec <= 90
  const radiusOk = radius == null || (Number.isFinite(radius) && radius > 0)
  const valid = form.name.trim() && raOk && decOk && radiusOk

  const submit = async (e) => {
    e.preventDefault()
    if (!valid) return
    setBusy(true); setError(null)
    try {
      const t = await createUserTarget({ name: form.name.trim(), ra, dec, radius_deg: radius })
      onCreated(t)
    } catch (err) {
      setError(errorText(err))
      setBusy(false)
    }
  }

  return (
    <Modal title="Add sky-position target"
           subtitle="Gathers every frame whose field of view covers this position."
           onClose={onClose}
           footer={<>
             <button className="btn-secondary" onClick={onClose}>Cancel</button>
             <button className="btn-primary" type="submit" form="add-target-form" disabled={!valid || busy}>
               {busy ? 'Adding…' : 'Add target'}
             </button>
           </>}>
      <form id="add-target-form" className="stack-sm" onSubmit={submit}>
        {error && <Alert>{error}</Alert>}
        <div className="field">
          <label htmlFor="t-name">Name</label>
          <input id="t-name" type="text" placeholder="e.g. Pelican Nebula" value={form.name} onChange={set('name')} />
        </div>
        <div className="form-grid">
          <div className="field">
            <label htmlFor="t-ra">RA (degrees)</label>
            <input id="t-ra" type="number" step="any" placeholder="e.g. 313.2" value={form.ra} onChange={set('ra')} />
            {form.ra !== '' && !raOk && <span className="field-hint text-danger">0 to 360</span>}
          </div>
          <div className="field">
            <label htmlFor="t-dec">Dec (degrees)</label>
            <input id="t-dec" type="number" step="any" placeholder="e.g. 44.4" value={form.dec} onChange={set('dec')} />
            {form.dec !== '' && !decOk && <span className="field-hint text-danger">−90 to +90</span>}
          </div>
        </div>
        <div className="field">
          <label htmlFor="t-radius">Radius (degrees) <span className="text-muted">(optional)</span></label>
          <input id="t-radius" type="number" step="any" placeholder="Blank = the exact point must be in frame"
                 value={form.radius} onChange={set('radius')} />
          {!radiusOk && <span className="field-hint text-danger">Must be greater than 0</span>}
        </div>
      </form>
    </Modal>
  )
}

export default function TargetsPage() {
  const [mode, setMode] = useState('header')     // 'header' | 'custom'
  const [sort, setSort] = useState('integration')
  const [loading, setLoading] = useState(false)
  const [loadError, setLoadError] = useState(null)

  const [headerData, setHeaderData] = useState(null)
  const [userData, setUserData] = useState(null)
  const [open, setOpen] = useState(null)
  const [detail, setDetail] = useState(null)
  const [adding, setAdding] = useState(false)
  const toast = useToast()
  const confirm = useConfirm()

  const loadHeader = useCallback(async () => {
    setLoading(true); setLoadError(null)
    try { setHeaderData(await getTargets()) } catch (e) { setLoadError(errorText(e)) } finally { setLoading(false) }
  }, [])
  const loadUser = useCallback(async () => {
    setLoading(true); setLoadError(null)
    try { setUserData(await getUserTargets()) } catch (e) { setLoadError(errorText(e)) } finally { setLoading(false) }
  }, [])

  useEffect(() => {
    setOpen(null); setDetail(null)
    if (mode === 'header') loadHeader(); else loadUser()
  }, [mode, loadHeader, loadUser])

  const toggle = async (key, loader) => {
    if (open === key) { setOpen(null); setDetail(null); return }
    setOpen(key); setDetail(null)
    try { setDetail(await loader()) } catch (e) { setDetail({ error: errorText(e) }) }
  }

  const handleCreated = (t) => {
    setAdding(false)
    toast(`Added “${t.name}”`, { tone: 'success' })
    loadUser()
  }

  const handleDelete = async (t) => {
    const ok = await confirm({
      title: 'Delete target?',
      message: `“${t.name}” will be removed. Frames are not affected.`,
      confirmLabel: 'Delete target',
      tone: 'danger',
    })
    if (!ok) return
    try { await deleteUserTarget(t.id); loadUser() } catch (e) { toast(errorText(e), { tone: 'danger' }) }
  }

  // Seed a custom target at the object's own median solved position
  const seedFromHeader = async (obj) => {
    try {
      const t = await createTargetFromObject(obj)
      setMode('custom')
      toast(`Created “${t.name}” at RA ${t.ra.toFixed(4)}° / Dec ${t.dec.toFixed(4)}° — it now gathers every frame covering that position.`,
            { tone: 'success', duration: 7000 })
    } catch (e) { toast(errorText(e), { tone: 'danger' }) }
  }

  const data = mode === 'header' ? headerData : userData
  const list = (data?.targets || []).slice().sort((a, b) => {
    const an = a.object || a.name, bn = b.object || b.name
    if (sort === 'name') return an.localeCompare(bn)
    if (sort === 'recent') return (b.last_night || '').localeCompare(a.last_night || '')
    if (sort === 'quality') return (b.avg_quality ?? -1) - (a.avg_quality ?? -1)
    return b.integration_sec - a.integration_sec
  })

  return (
    <div className="page">
      <PageHeader
        title="Targets"
        subtitle={data
          ? `${plural(data.target_count, 'target')} · ${fmtHours(data.total_integration_sec)} total integration`
          : 'Integration time per target, filter and night'}>
        {mode === 'custom' && (
          <button className="btn-primary" onClick={() => setAdding(true)}>
            <Icon name="plus" size={15} />Add target
          </button>
        )}
      </PageHeader>

      <div className="page-body">
        <div className="stack constrain">
          <div className="toolbar">
            <div className="segmented" role="tablist" aria-label="Target source">
              <button role="tab" aria-selected={mode === 'header'} className={mode === 'header' ? 'active' : ''}
                      onClick={() => setMode('header')}>
                <Icon name="file" size={14} />From FITS header
              </button>
              <button role="tab" aria-selected={mode === 'custom'} className={mode === 'custom' ? 'active' : ''}
                      onClick={() => setMode('custom')}>
                <Icon name="pin" size={14} />Custom sky positions
              </button>
            </div>
            <span className="spacer" />
            <label htmlFor="tg-sort">Sort by</label>
            <select id="tg-sort" value={sort} onChange={e => setSort(e.target.value)}>
              <option value="integration">Integration time</option>
              <option value="recent">Most recent</option>
              <option value="quality">Quality</option>
              <option value="name">Name</option>
            </select>
          </div>

          {mode === 'custom' && (
            <p className="note">
              <Icon name="info" size={15} />
              <span>
                Custom targets match <b>any frame whose field of view covers the position</b>, so one image can count
                toward several targets regardless of its OBJECT header. Frames need coordinates — plate-solved frames
                are the most accurate.
              </span>
            </p>
          )}

          {loadError && <Alert>Couldn't load targets: {loadError}</Alert>}
          {loading && !data && <LoadingBlock>Loading targets…</LoadingBlock>}

          {data && list.length === 0 && !loading && (
            mode === 'header' ? (
              <EmptyState icon="target" title="No targets yet">
                Targets appear once light frames with an OBJECT header are indexed.
              </EmptyState>
            ) : (
              <EmptyState icon="pin" title="No custom targets"
                          action={<button className="btn-primary" onClick={() => setAdding(true)}><Icon name="plus" size={15} />Add target</button>}>
                Add a position by RA / Dec, or use “Track position” on a header target.
              </EmptyState>
            )
          )}

          <div className="tg-list">
            {mode === 'header' && list.map(t => (
              <TargetCard
                key={t.object} t={t}
                isOpen={open === t.object}
                detail={open === t.object ? detail : null}
                onToggle={() => toggle(t.object, () => getTarget(t.object))}
                actions={
                  <button className="btn-ghost btn-sm" onClick={() => seedFromHeader(t.object)}
                          title="Create a custom sky-position target from this object">
                    <Icon name="pin" size={13} />Track position
                  </button>
                }
              />
            ))}

            {mode === 'custom' && list.map(t => (
              <TargetCard
                key={t.id} t={t}
                subtitle={
                  <span className="tg-coords">
                    RA {t.ra?.toFixed(4)}° · Dec {t.dec?.toFixed(4)}°
                    {t.radius_deg ? ` · radius ${t.radius_deg}°` : ' · point match'}
                  </span>
                }
                isOpen={open === t.id}
                detail={open === t.id ? detail : null}
                onToggle={() => toggle(t.id, () => getUserTargetDetail(t.id))}
                actions={
                  <button className="icon-btn danger" onClick={() => handleDelete(t)}
                          title="Delete target" aria-label={`Delete ${t.name}`}>
                    <Icon name="trash" size={15} />
                  </button>
                }
              />
            ))}
          </div>
        </div>
      </div>

      {adding && <AddTargetDialog onClose={() => setAdding(false)} onCreated={handleCreated} />}
    </div>
  )
}
