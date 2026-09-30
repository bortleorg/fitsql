import { useState, useEffect, useCallback } from 'react'
import { getSessions, errorText } from '../components/api.js'
import Icon from '../components/Icon.jsx'
import { Alert, EmptyState, FilterChip, LoadingBlock, PageHeader, Spinner, Stat } from '../components/ui.jsx'
import { fmtHours, plural, qualityColor } from '../components/format.js'

// Sessions grouped under their night, keeping the API's order
function byNight(sessions) {
  const groups = new Map()
  for (const s of sessions) {
    if (!groups.has(s.night)) groups.set(s.night, [])
    groups.get(s.night).push(s)
  }
  return [...groups.entries()].map(([night, list]) => ({
    night,
    sessions: list,
    integration: list.reduce((n, s) => n + (s.integration_sec || 0), 0),
  }))
}

export default function SessionsPage() {
  const [data, setData] = useState(null)
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState(null)

  const fetchSessions = useCallback(async () => {
    setLoading(true)
    setError(null)
    try {
      setData(await getSessions())
    } catch (e) {
      setError(errorText(e))
    } finally {
      setLoading(false)
    }
  }, [])

  useEffect(() => { fetchSessions() }, [fetchSessions])

  const nights = data ? byNight(data.sessions) : []
  const frames = data ? data.sessions.reduce((n, s) => n + (s.frames || 0), 0) : 0

  return (
    <div className="page">
      <PageHeader title="Sessions" subtitle="Imaging sessions per target and night, from indexed light frames">
        <button className="btn-secondary btn-sm" onClick={fetchSessions} disabled={loading}>
          {loading ? <Spinner size={12} /> : <Icon name="refresh" size={13} />}Refresh
        </button>
      </PageHeader>

      <div className="page-body">
        <div className="stack constrain">
          {error && <Alert>Couldn't load sessions: {error}</Alert>}
          {loading && !data && <LoadingBlock>Loading sessions…</LoadingBlock>}

          {data && data.sessions.length > 0 && (
            <div className="stats-row">
              <Stat label="Total integration" value={fmtHours(data.total_integration_sec)} tone="success" />
              <Stat label="Sessions" value={data.session_count.toLocaleString()} />
              <Stat label="Nights" value={nights.length.toLocaleString()} />
              <Stat label="Light frames" value={frames.toLocaleString()} />
            </div>
          )}

          {data && data.sessions.length === 0 && !loading && (
            <EmptyState icon="moon" title="No imaging sessions yet">
              Sessions are built from indexed light frames that have a DATE-OBS header.
            </EmptyState>
          )}

          <div>
            {nights.map(g => (
              <section key={g.night} className="night-group">
                <div className="night-heading">
                  <h2>{g.night}</h2>
                  <span>{plural(g.sessions.length, 'session')} · {fmtHours(g.integration)}</span>
                </div>

                {g.sessions.map((s, i) => (
                  <div key={i} className="session-card">
                    <div className="session-head">
                      <div className="session-title">
                        <span className="session-object">{s.object}</span>
                        {(s.telescop || s.instrume) && (
                          <span className="session-gear">{[s.telescop, s.instrume].filter(Boolean).join(' · ')}</span>
                        )}
                      </div>
                      <div className="session-totals">
                        <span className="session-integration">{fmtHours(s.integration_sec)}</span>
                        <span className="session-frames">{plural(s.frames, 'frame')}</span>
                      </div>
                    </div>
                    <div className="table-wrap">
                      <table className="data-table compact">
                        <thead>
                          <tr>
                            <th>Filter</th><th className="num">Frames</th><th className="num">Integration</th>
                            <th className="num">Avg HFR</th><th className="num">Avg Ecc</th><th className="num">Avg Quality</th>
                          </tr>
                        </thead>
                        <tbody>
                          {s.filters.map((f, j) => (
                            <tr key={j}>
                              <td><FilterChip name={f.filter} /></td>
                              <td className="num">{f.frames}</td>
                              <td className="num">{fmtHours(f.integration_sec)}</td>
                              <td className="num">{f.avg_hfr != null ? `${f.avg_hfr} px` : <span className="dim">—</span>}</td>
                              <td className="num">{f.avg_eccentricity ?? <span className="dim">—</span>}</td>
                              <td className="num" style={{ color: qualityColor(f.avg_quality), fontWeight: 600 }}>
                                {f.avg_quality ?? '—'}
                              </td>
                            </tr>
                          ))}
                        </tbody>
                      </table>
                    </div>
                  </div>
                ))}
              </section>
            ))}
          </div>
        </div>
      </div>
    </div>
  )
}
