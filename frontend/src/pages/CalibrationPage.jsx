import { useCallback, useEffect, useState } from 'react'
import { getCalibration, errorText } from '../components/api.js'
import Icon from '../components/Icon.jsx'
import { Alert, EmptyState, FilterChip, LoadingBlock, PageHeader, Spinner, Stat } from '../components/ui.jsx'
import { fmtHours } from '../components/format.js'

function Count({ n, warn }) {
  if (!n) return <span className="badge badge-danger"><Icon name="x" size={11} strokeWidth={2.5} />None</span>
  return (
    <span className={`badge ${warn ? 'badge-warning' : 'badge-success'}`}>
      <Icon name={warn ? 'alert' : 'check'} size={11} strokeWidth={2.5} />{n}
    </span>
  )
}

export default function CalibrationPage() {
  const [data, setData] = useState(null)
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState(null)
  const [onlyProblems, setOnlyProblems] = useState(false)

  const load = useCallback(async () => {
    setLoading(true)
    setError(null)
    try {
      setData(await getCalibration())
    } catch (e) {
      setError(errorText(e))
    } finally {
      setLoading(false)
    }
  }, [])

  useEffect(() => { load() }, [load])

  const setups = data ? data.setups.filter(s => !onlyProblems || s.missing.length || s.stale_flats) : []
  const summary = data?.summary
  const cal = data?.calibration_frames

  return (
    <div className="page">
      <PageHeader title="Calibration" subtitle="Check that every light setup has matching darks, flats and bias">
        <button className="btn-secondary btn-sm" onClick={load} disabled={loading}>
          {loading ? <Spinner size={12} /> : <Icon name="refresh" size={13} />}Refresh
        </button>
      </PageHeader>

      <div className="page-body">
        <div className="stack constrain">
          {error && <Alert>Couldn't load calibration coverage: {error}</Alert>}
          {loading && !data && <LoadingBlock>Checking calibration coverage…</LoadingBlock>}

          {data && (
            <div className="stats-row">
              <Stat label="Setups fully covered" value={`${summary.complete} / ${summary.setups}`}
                    tone={summary.setups && summary.complete === summary.setups ? 'success' : 'warning'} />
              <Stat label="Lights missing darks or flats" value={fmtHours(summary.uncalibrated_integration_sec)}
                    tone={summary.uncalibrated_integration_sec ? 'danger' : 'success'} />
              <Stat label="Darks" value={cal.darks.toLocaleString()} />
              <Stat label="Flats" value={cal.flats.toLocaleString()} />
              <Stat label="Bias" value={cal.bias.toLocaleString()} />
            </div>
          )}

          {data && data.setups.length === 0 && !loading && (
            <EmptyState icon="flask" title="No light frames indexed yet">
              Coverage is checked once lights, darks, flats and bias are in the catalog.
            </EmptyState>
          )}

          {data && data.setups.length > 0 && (
            <section className="card">
              <div className="card-header">
                <div>
                  <h2 className="card-title">Light setups</h2>
                  <p className="card-subtitle">
                    {onlyProblems ? `${setups.length} of ${data.setups.length} setups need attention` : `${data.setups.length} setups`}
                  </p>
                </div>
                <label className="checkbox">
                  <input type="checkbox" checked={onlyProblems} onChange={e => setOnlyProblems(e.target.checked)} />
                  Only setups with problems
                </label>
              </div>

              {setups.length === 0 ? (
                <EmptyState icon="check" title="Everything is covered">
                  Every light setup has matching darks and fresh flats.
                </EmptyState>
              ) : (
                <div className="table-wrap">
                  <table className="data-table cal-table">
                    <thead>
                      <tr>
                        <th>Camera</th><th>Filter</th><th className="num">Exposure</th><th className="num">Gain / Offset</th>
                        <th className="num">Bin</th><th className="num">Temp</th><th className="num">Lights</th>
                        <th className="num">Nights</th><th>Targets</th><th>Darks</th><th>Flats</th><th>Bias</th>
                      </tr>
                    </thead>
                    <tbody>
                      {setups.map((s, i) => (
                        <tr key={i} className={s.missing.length ? 'cal-row-bad' : s.stale_flats ? 'cal-row-warn' : ''}>
                          <td>{s.camera}</td>
                          <td><FilterChip name={s.filter} /></td>
                          <td className="num">{s.exposure_s != null ? `${s.exposure_s}s` : '—'}</td>
                          <td className="num">{s.gain ?? '—'} / {s.offset ?? '—'}</td>
                          <td className="num">{s.binning}×{s.binning}</td>
                          <td className="num">{s.sensor_temp_c != null ? `${s.sensor_temp_c}°C` : '—'}</td>
                          <td className="num">{s.frames} <span className="dim">· {fmtHours(s.integration_sec)}</span></td>
                          <td className="num" title={s.first_night ? `${s.first_night} → ${s.last_night}` : ''}>{s.nights}</td>
                          <td className="wrap">{s.objects.join(', ') || <span className="dim">—</span>}</td>
                          <td><Count n={s.darks} /></td>
                          <td>
                            <Count n={s.flats} warn={s.stale_flats} />
                            {s.flat_gap_days != null && (
                              <span className="cal-gap" title="Worst gap from a light night to the nearest flat night">
                                {s.flat_gap_days}d
                              </span>
                            )}
                          </td>
                          <td>
                            {s.bias > 0
                              ? <span className="badge badge-success"><Icon name="check" size={11} strokeWidth={2.5} />{s.bias}</span>
                              : <span className="dim">—</span>}
                          </td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
              )}
            </section>
          )}

          {data && (
            <p className="note">
              <Icon name="info" size={15} />
              <span>
                Darks match camera, binning, gain, offset and exposure within {data.tolerances.temp_c}°C. Flats match
                camera, binning and filter; flats more than {data.tolerances.flat_max_days} days from a light night are
                flagged as stale.
              </span>
            </p>
          )}
        </div>
      </div>
    </div>
  )
}
