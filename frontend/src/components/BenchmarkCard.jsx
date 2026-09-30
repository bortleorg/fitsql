import { useState, useEffect, useRef, useCallback } from 'react'
import { benchmarkPick, benchmarkRun, getBenchmark, errorText } from './api.js'
import Icon from './Icon.jsx'
import { Alert, copyText, useToast } from './ui.jsx'

const ms = (s) => (s == null ? '—' : `${(s * 1000).toFixed(0)} ms`)

function buildReport(res, phases) {
  const per = res.per_dtype, sp = res.speedup, cmp = res.comparison
  const L = []
  L.push('## fitsql — float32 vs float64 benchmark')
  L.push(`Sample: ${res.runs} runs, ~${res.megapixels ?? '?'} MP/frame. dtype affects thumb+quality only.`)
  L.push('')
  L.push('### Timing (mean per frame)')
  L.push('| phase | float32 | float64 | f32 speedup |')
  L.push('|---|---|---|---|')
  phases.forEach(p => L.push(`| ${p} | ${ms(per.float32?.[p])} | ${ms(per.float64?.[p])} | ${sp?.[p] != null ? sp[p] + '×' : '—'} |`))
  L.push('')
  L.push(`### Metric accuracy (float32 vs float64) — ${cmp?.ok ? 'MATCH ✅' : 'DIFFERENCES ⚠️'}`)
  L.push('| metric | max rel diff | tol | flagged |')
  L.push('|---|---|---|---|')
  if (cmp?.per_metric) Object.entries(cmp.per_metric).forEach(([m, s]) =>
    L.push(`| ${m} | ${s.max_rel_pct}% | ${s.tol_pct}% | ${s.flagged ? 'YES' : 'no'} |`))
  if (cmp?.flags?.length) { L.push(''); L.push('Flags:'); cmp.flags.forEach(f => L.push(`- ${f}`)) }
  L.push('')
  L.push('Question: is float32 a net win on perf, and are the metric differences acceptable (no quality regression)? If a phase dominates, suggest the next optimization.')
  return L.join('\n')
}

export default function BenchmarkCard({ id }) {
  const [data, setData] = useState(null)
  const [n, setN] = useState(10)
  const [dtypes, setDtypes] = useState('float32,float64')
  const [busy, setBusy] = useState(false)
  const timer = useRef(null)
  const toast = useToast()

  const poll = useCallback(async () => {
    try { setData(await getBenchmark()) } catch { /* ignore */ }
  }, [])

  useEffect(() => {
    poll()
    timer.current = setInterval(poll, 2000)
    return () => clearInterval(timer.current)
  }, [poll])

  const handlePick = async () => {
    setBusy(true)
    try { const r = await benchmarkPick(n); toast(`Selected ${r.count} files for the benchmark`, { tone: 'success' }); poll() }
    catch (e) { toast(errorText(e), { tone: 'danger' }) } finally { setBusy(false) }
  }
  const handleRun = async () => {
    setBusy(true)
    try { await benchmarkRun(1, dtypes); toast('Benchmark queued — run it while the queue is idle for clean numbers', { tone: 'success' }) }
    catch (e) { toast(errorText(e), { tone: 'danger' }) } finally { setBusy(false) }
  }
  const handleCopy = () => {
    copyText(buildReport(res, phases))
      .then(() => toast('Report copied', { tone: 'success' }))
      .catch(() => toast('Could not copy to the clipboard', { tone: 'danger' }))
  }

  const files = data?.files || []
  const prog = data?.progress
  const res = data?.result
  const per = res?.per_dtype
  const speed = res?.speedup
  const phases = data?.phases || []
  // The phase that costs the most (float32) — the optimization target
  const hot = per?.float32
    ? phases.filter(p => p !== 'total').reduce((a, p) =>
        (per.float32[p] || 0) > (per.float32[a] || 0) ? p : a, 'read')
    : null

  return (
    <section id={id} className="card settings-card">
      <div className="card-header">
        <div>
          <h2 className="card-title">Benchmark</h2>
          <p className="card-subtitle">Time the pipeline on a fixed sample — float32 vs float64, per phase</p>
        </div>
      </div>
      <div className="card-body">
        <div className="form-row">
          <label className="checkbox" htmlFor="bench-n">Sample size</label>
          <input id="bench-n" type="number" min="1" max="50" value={n} style={{ width: 72 }}
                 onChange={e => setN(parseInt(e.target.value) || 10)} />
          <button className="btn-secondary" onClick={handlePick} disabled={busy}>Pick random files</button>
          <select value={dtypes} onChange={e => setDtypes(e.target.value)} aria-label="Data types to compare">
            <option value="float32,float64">float32 vs float64</option>
            <option value="float32">float32 only (all frames)</option>
          </select>
          <button className="btn-primary" onClick={handleRun} disabled={busy || files.length === 0}>
            <Icon name="play" size={14} />Run benchmark
          </button>
          <span className="help-text">{files.length} files in set</span>
        </div>

        {prog && prog.done < prog.total && (
          <div className="stack-sm">
            <div className="progress"><div className="progress-fill" style={{ width: `${Math.round(prog.done / prog.total * 100)}%` }} /></div>
            <span className="help-text">Running {prog.done} of {prog.total}…</span>
          </div>
        )}

        {res && per && (
          <>
            <p className="help-text">
              {res.compared != null ? `${res.compared} frames compared` : `${res.runs} runs`}
              {res.megapixels ? ` · avg ${res.megapixels} MP` : ''} · slowest phase: <b className="text-accent">{hot}</b>
              {res.skipped ? <span className="text-warning"> · {res.skipped} float64 runs skipped and excluded (too big for the memory limit — use float32 only to time them)</span> : null}
              {res.errors ? <span className="text-danger"> · {res.errors} errors</span> : null}
            </p>
            <div className="table-frame">
              <table className="data-table compact">
                <thead>
                  <tr><th>Phase</th><th className="num">float32</th><th className="num">float64</th><th className="num">f32 speedup</th></tr>
                </thead>
                <tbody>
                  {phases.map(p => (
                    <tr key={p} className={p === 'total' ? 'row-strong' : p === hot ? 'row-accent' : ''}>
                      <td className="mono">{p}</td>
                      <td className="num">{ms(per.float32?.[p])}</td>
                      <td className="num">{ms(per.float64?.[p])}</td>
                      <td className="num">{speed?.[p] != null ? `${speed[p]}×` : '—'}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
            <p className="help-text">
              dtype only affects <code>thumb</code> and <code>quality</code> (read / wcs / sky are dtype-independent).
              SEP sub-phases <code>sep_bg</code>, <code>extract</code> and <code>hfr</code> roll up into <code>quality</code>.
            </p>

            {res.comparison && (
              <>
                <Alert tone={res.comparison.ok ? 'success' : 'warning'}>
                  {res.comparison.ok
                    ? 'float32 metrics match float64 within tolerance'
                    : `${res.comparison.flags.length} metric(s) differ beyond tolerance — review before keeping float32`}
                </Alert>
                <div className="table-frame">
                  <table className="data-table compact">
                    <thead>
                      <tr><th>Metric</th><th className="num">Max Δ</th><th className="num">Tolerance</th><th>Status</th></tr>
                    </thead>
                    <tbody>
                      {Object.entries(res.comparison.per_metric).map(([m, s]) => (
                        <tr key={m} className={s.flagged ? 'row-danger' : ''}>
                          <td className="mono">{m}</td>
                          <td className="num">{s.max_rel_pct}%</td>
                          <td className="num">{s.tol_pct}%</td>
                          <td>{s.flagged
                            ? <span className="badge badge-danger">Differs</span>
                            : <span className="badge badge-success">Match</span>}</td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
                {res.comparison.flags?.map((f, i) => <p key={i} className="help-text text-danger">{f}</p>)}
              </>
            )}

            <div className="row">
              <button className="btn-secondary btn-sm" onClick={handleCopy}>
                <Icon name="copy" size={13} />Copy report for a coding agent
              </button>
            </div>
          </>
        )}
      </div>
    </section>
  )
}
