import { BASE } from './api.js'
import Icon from './Icon.jsx'
import { FilterChip } from './ui.jsx'
import { fixed, qualityColor } from './format.js'

function Metric({ label, value, bad }) {
  return (
    <div className={`card-metric${bad ? ' bad' : ''}`}>
      <span>{label}</span>
      {value == null ? <b className="none">—</b> : <b>{value}</b>}
    </div>
  )
}

export default function FileCard({ file, onClick, onCompare, inCompare }) {
  const thumbSrc = file.thumbnail_url ? `${BASE}${file.thumbnail_url}` : null
  const isLight = !file.imagetyp || /light/i.test(file.imagetyp)
  const measured = file.median_hfr != null || file.star_count != null || file.eccentricity != null
  const open = () => onClick(file)

  return (
    <div className={`file-card${file.rejected === 1 ? ' card-rejected' : ''}${inCompare ? ' in-compare' : ''}`}
         role="button" tabIndex={0} aria-label={`Open ${file.filename}`}
         onClick={open}
         onKeyDown={e => {
           if ((e.key === 'Enter' || e.key === ' ') && e.target === e.currentTarget) { e.preventDefault(); open() }
         }}>
      <div className="card-thumb">
        {thumbSrc ? (
          <img src={thumbSrc} alt="" loading="lazy" draggable={false} />
        ) : (
          <div className="card-thumb-placeholder">
            <Icon name="image" size={24} />
            <span>No preview</span>
          </div>
        )}

        {(file.rejected === 1 || file.rejected === 0 || file.trailed) ? (
          <div className="card-flags">
            {file.rejected === 1 && (
              <span className="card-flag flag-rejected"><Icon name="x" size={10} strokeWidth={3} />REJECTED</span>
            )}
            {file.rejected === 0 && (
              <span className="card-flag flag-accepted"><Icon name="check" size={10} strokeWidth={3} />ACCEPTED</span>
            )}
            {file.trailed ? (
              <span className="card-flag flag-trailed"
                    title={`Star trailing detected (trail score ${fixed(file.trail_score, 3)})`}>
                TRAILED
              </span>
            ) : null}
          </div>
        ) : null}

        {file.quality_score != null && (
          <span className="card-overlay card-quality" style={{ color: qualityColor(file.quality_score) }}
                title="Quality score (0–100)">
            {Math.round(file.quality_score)}
          </span>
        )}
        {file.streaks > 0 && (
          <span className="card-overlay card-streak"
                title={`${file.streaks} satellite/aircraft streak${file.streaks > 1 ? 's' : ''}`}>
            <Icon name="streak" size={12} />{file.streaks}
          </span>
        )}
        {onCompare && (
          <button className={`card-compare${inCompare ? ' on' : ''}`}
                  title={inCompare ? 'Remove from comparison' : 'Add to comparison'}
                  aria-label={inCompare ? 'Remove from comparison' : 'Add to comparison'}
                  aria-pressed={!!inCompare}
                  onClick={e => { e.stopPropagation(); onCompare(file) }}>
            <Icon name="columns" size={14} />
          </button>
        )}
      </div>

      <div className="card-body">
        <div className="card-filename" title={file.filename}>{file.filename}</div>
        <div className="card-sub">
          {file.object && <span className="card-object" title={file.object}>{file.object}</span>}
          {file.filter && <FilterChip name={file.filter} />}
          {file.exptime != null && <span className="dim">{file.exptime}s</span>}
          {!isLight && <span className="badge">{file.imagetyp}</span>}
        </div>
        <div className="card-date">
          <span>{file.date_obs ? file.date_obs.slice(0, 16).replace('T', ' ') : 'No date'}</span>
          {file.focal_length != null && <span>{file.focal_length} mm</span>}
        </div>
        {measured && (
          <div className="card-metrics">
            <Metric label="HFR" value={fixed(file.median_hfr)} />
            <Metric label="FWHM"
                    value={file.fwhm_arcsec != null ? `${fixed(file.fwhm_arcsec)}″` : fixed(file.median_fwhm)} />
            <Metric label="Ecc" value={fixed(file.eccentricity)} />
            <Metric label="Stars" value={file.star_count != null ? file.star_count.toLocaleString() : null} />
            <Metric label="Trail" value={fixed(file.trail_score, 3)} bad={!!file.trailed} />
            <Metric label="Alt" value={file.altitude != null ? `${file.altitude.toFixed(0)}°` : null} />
          </div>
        )}
      </div>
    </div>
  )
}
