import { useState } from 'react'
import Icon from './Icon.jsx'

const DEFAULT_FILTERS = {
  object: '',
  filter: '',
  imagetyp: 'Light Frame',
  telescop: '',
  instrume: '',
  date_from: '',
  date_to: '',
  focal_min: '',
  focal_max: '',
  exptime_min: '',
  exptime_max: '',
  hfr_max: '',
  ecc_max: '',
  stars_min: '',
  tracking: '',
  path_like: '',
  path_not_like: '',
}

function Options({ values = [] }) {
  return values.map(v => <option key={v} value={v}>{v}</option>)
}

export default function FilterPanel({ stats, onApply }) {
  const [values, setValues] = useState({ ...DEFAULT_FILTERS })

  const set = (key, val) => setValues(v => ({ ...v, [key]: val }))
  const bind = (key) => ({ value: values[key], onChange: e => set(key, e.target.value) })

  // A form so Enter in any field applies
  const handleSubmit = (e) => {
    e.preventDefault()
    onApply({ ...values })
  }

  const handleClear = () => {
    setValues({ ...DEFAULT_FILTERS })
    onApply({ ...DEFAULT_FILTERS })
  }

  const distinct = stats?.distinct || {}

  return (
    <form className="filter-sidebar" onSubmit={handleSubmit} aria-label="Catalog filters">
      <div className="filter-scroll">
        <section className="filter-section">
          <h2 className="filter-section-title"><Icon name="search" size={12} />Search</h2>
          <div className="field">
            <label htmlFor="f-object">Object name</label>
            <input id="f-object" type="text" placeholder="e.g. M31" {...bind('object')} />
          </div>
          <div className="field">
            <label htmlFor="f-path-like" title="SQL LIKE pattern (% wildcard). Plain text = contains.">Path includes</label>
            <input id="f-path-like" type="text" placeholder="e.g. M31 or %/2026-%" {...bind('path_like')} />
          </div>
          <div className="field">
            <label htmlFor="f-path-not" title="SQL LIKE pattern (% wildcard). Plain text = contains.">Path excludes</label>
            <input id="f-path-not" type="text" placeholder="e.g. reject" {...bind('path_not_like')} />
          </div>
        </section>

        <section className="filter-section">
          <h2 className="filter-section-title"><Icon name="image" size={12} />Capture</h2>
          <div className="field">
            <label htmlFor="f-imagetyp">Image type</label>
            <select id="f-imagetyp" {...bind('imagetyp')}>
              <option value="">All types</option>
              <Options values={distinct.imagetypes} />
            </select>
          </div>
          <div className="field">
            <label htmlFor="f-filter">Filter</label>
            <select id="f-filter" {...bind('filter')}>
              <option value="">All filters</option>
              <Options values={distinct.filters} />
            </select>
          </div>
          <div className="field">
            <label htmlFor="f-telescop">Telescope</label>
            <select id="f-telescop" {...bind('telescop')}>
              <option value="">All telescopes</option>
              <Options values={distinct.telescopes} />
            </select>
          </div>
          <div className="field">
            <label htmlFor="f-instrume">Camera</label>
            <select id="f-instrume" {...bind('instrume')}>
              <option value="">All cameras</option>
              <Options values={distinct.instruments} />
            </select>
          </div>
          <div className="field">
            <label htmlFor="f-date-from" title="Capture time (DATE-OBS, UTC)">From (UTC)</label>
            <input id="f-date-from" type="text" placeholder="YYYY-MM-DD [HH:MM]" {...bind('date_from')} />
          </div>
          <div className="field">
            <label htmlFor="f-date-to" title="Inclusive at the precision typed: a date includes that whole day">To (UTC)</label>
            <input id="f-date-to" type="text" placeholder="YYYY-MM-DD [HH:MM]" {...bind('date_to')} />
          </div>
          <div className="field">
            <label>Focal length (mm)</label>
            <div className="filter-range">
              <input type="number" placeholder="Min" aria-label="Minimum focal length" {...bind('focal_min')} />
              <span>–</span>
              <input type="number" placeholder="Max" aria-label="Maximum focal length" {...bind('focal_max')} />
            </div>
          </div>
          <div className="field">
            <label>Exposure (s)</label>
            <div className="filter-range">
              <input type="number" placeholder="Min" aria-label="Minimum exposure" {...bind('exptime_min')} />
              <span>–</span>
              <input type="number" placeholder="Max" aria-label="Maximum exposure" {...bind('exptime_max')} />
            </div>
          </div>
        </section>

        <section className="filter-section">
          <h2 className="filter-section-title"><Icon name="gauge" size={12} />Quality</h2>
          <div className="field">
            <label htmlFor="f-hfr">Max HFR (px)</label>
            <input id="f-hfr" type="number" step="0.1" placeholder="e.g. 3.5" {...bind('hfr_max')} />
          </div>
          <div className="field">
            <label htmlFor="f-ecc">Max eccentricity</label>
            <input id="f-ecc" type="number" step="0.05" placeholder="e.g. 0.6" {...bind('ecc_max')} />
          </div>
          <div className="field">
            <label htmlFor="f-stars">Min stars</label>
            <input id="f-stars" type="number" placeholder="e.g. 500" {...bind('stars_min')} />
          </div>
          <div className="field">
            <label htmlFor="f-tracking"
                   title="From trail score: stars smeared the same way (mount slip / bad tracking). Unanalyzed frames match neither.">
              Tracking
            </label>
            <select id="f-tracking" {...bind('tracking')}>
              <option value="">Any</option>
              <option value="good">Good tracking only</option>
              <option value="trailed">Trailed only</option>
            </select>
          </div>
        </section>
      </div>

      <div className="filter-footer">
        <button type="button" className="btn-secondary" onClick={handleClear}>Reset</button>
        <button type="submit" className="btn-primary">Apply</button>
      </div>
    </form>
  )
}
