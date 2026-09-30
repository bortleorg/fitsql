import { useEffect, useState } from 'react'
import CatalogPage from './pages/CatalogPage.jsx'
import SelectorPage from './pages/SelectorPage.jsx'
import TargetsPage from './pages/TargetsPage.jsx'
import SessionsPage from './pages/SessionsPage.jsx'
import CalibrationPage from './pages/CalibrationPage.jsx'
import SettingsPage from './pages/SettingsPage.jsx'
import Icon from './components/Icon.jsx'
import { ConfirmProvider, ToastProvider } from './components/ui.jsx'
import { BASE, getQueue } from './components/api.js'
import { plural } from './components/format.js'

const NAV = [
  {
    section: 'Library',
    items: [
      { key: 'catalog', label: 'Catalog', icon: 'grid', page: CatalogPage },
      { key: 'selector', label: 'Selector', icon: 'sliders', page: SelectorPage },
      { key: 'targets', label: 'Targets', icon: 'target', page: TargetsPage },
      { key: 'sessions', label: 'Sessions', icon: 'moon', page: SessionsPage },
      { key: 'calibration', label: 'Calibration', icon: 'flask', page: CalibrationPage },
    ],
  },
  {
    section: 'System',
    items: [{ key: 'settings', label: 'Settings', icon: 'settings', page: SettingsPage }],
  },
]
const PAGES = Object.fromEntries(NAV.flatMap(s => s.items).map(it => [it.key, it]))

// The page lives in the URL hash so reloads and the back button keep your place
const pageFromHash = () => {
  const key = window.location.hash.replace(/^#\/?/, '').split(/[/?]/)[0]
  return PAGES[key] ? key : 'catalog'
}

function QueueStatus({ onClick }) {
  const [q, setQ] = useState(null)

  // Next poll only after the last one settles — an unreachable Redis makes /api/queue
  // slow, and overlapping requests would eat the browser's per-host connection limit
  useEffect(() => {
    let live = true
    let timer
    const poll = () => getQueue()
      .then(r => { if (live) setQ(r) })
      .catch(() => { if (live) setQ({ available: false }) })
      .finally(() => { if (live) timer = setTimeout(poll, 5000) })
    poll()
    return () => { live = false; clearTimeout(timer) }
  }, [])

  const state = !q ? 'unknown' : !q.available ? 'off' : q.active ? 'busy' : 'idle'
  const label = {
    unknown: ['Job queue', 'Checking…'],
    off: ['Queue offline', 'Workers unavailable'],
    busy: ['Processing', `${(q?.pending ?? 0).toLocaleString()} queued · ${q?.inflight ?? 0} running`],
    idle: ['Queue idle', plural(q?.workers ?? 0, 'worker')],
  }[state]

  return (
    <button className="queue-status" onClick={onClick} title="Job queue — open Settings">
      <span className={`dot dot-${state}`} />
      <span>{label[0]}<small>{label[1]}</small></span>
    </button>
  )
}

export default function App() {
  const [page, setPage] = useState(pageFromHash)

  useEffect(() => {
    const onHash = () => setPage(pageFromHash())
    window.addEventListener('hashchange', onHash)
    return () => window.removeEventListener('hashchange', onHash)
  }, [])
  useEffect(() => { document.title = `${PAGES[page].label} · fitsql` }, [page])

  const go = (key) => { window.location.hash = `/${key}` }
  const Page = PAGES[page].page

  return (
    <ToastProvider>
      <ConfirmProvider>
        <div className="app-shell">
          <aside className="sidebar">
            <div className="brand">
              <div className="brand-mark"><Icon name="logo" size={18} strokeWidth={2} /></div>
              <div>
                <div className="brand-name">fitsql</div>
                <div className="brand-sub">Query your FITS frames</div>
              </div>
            </div>
            <nav className="nav" aria-label="Main">
              {NAV.map(section => (
                <div key={section.section} className="nav-section">
                  <div className="nav-label">{section.section}</div>
                  {section.items.map(it => (
                    <button key={it.key} className={`nav-btn${page === it.key ? ' active' : ''}`}
                            aria-current={page === it.key ? 'page' : undefined}
                            onClick={() => go(it.key)} title={it.label}>
                      <Icon name={it.icon} />
                      <span>{it.label}</span>
                    </button>
                  ))}
                  {section.section === 'System' && (
                    <a className="nav-btn" href={`${BASE}/api/v1/docs`} target="_blank" rel="noreferrer" title="API docs">
                      <Icon name="code" />
                      <span>API docs</span>
                      <Icon name="external" size={13} className="nav-trail" />
                    </a>
                  )}
                </div>
              ))}
            </nav>
            <div className="sidebar-footer">
              <QueueStatus onClick={() => go('settings')} />
            </div>
          </aside>
          <main className="main-content">
            <Page key={page} />
          </main>
        </div>
      </ConfirmProvider>
    </ToastProvider>
  )
}
