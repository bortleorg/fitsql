import { useState, useEffect, useCallback, useRef } from 'react'
import FilterPanel from '../components/FilterPanel.jsx'
import FileCard from '../components/FileCard.jsx'
import FileDetail from '../components/FileDetail.jsx'
import useGrading from '../components/useGrading.js'
import CompareView, { CompareTray, useCompare } from '../components/CompareView.jsx'
import Icon from '../components/Icon.jsx'
import { Alert, CardSkeletons, EmptyState, LoadingBlock, PageHeader, Pagination, Spinner } from '../components/ui.jsx'
import { getFiles, getStats, errorText } from '../components/api.js'

const PER_PAGE = 50

const SORTS = [
  ['date_obs', 'Capture date'],
  ['quality_score', 'Quality'],
  ['median_hfr', 'HFR'],
  ['median_fwhm', 'FWHM'],
  ['eccentricity', 'Eccentricity'],
  ['trail_score', 'Trail score'],
  ['star_count', 'Star count'],
  ['exptime', 'Exposure'],
  ['focal_length', 'Focal length'],
  ['filename', 'Filename'],
  ['indexed_at', 'Date indexed'],
]

export default function CatalogPage() {
  const [files, setFiles] = useState([])
  const [total, setTotal] = useState(0)
  const [page, setPage] = useState(1)
  const [pages, setPages] = useState(1)
  const [filters, setFilters] = useState({ imagetyp: 'Light Frame' })
  const [sort, setSort] = useState('date_obs')
  const [order, setOrder] = useState('desc')
  const [stats, setStats] = useState(null)
  const [loading, setLoading] = useState(false)
  const [loadedOnce, setLoadedOnce] = useState(false)
  const [error, setError] = useState(null)
  const [failedView, setFailedView] = useState(false)
  const grading = useGrading(files, setFiles)
  const compare = useCompare()
  const scrollRef = useRef(null)

  const fetchFiles = useCallback(async (activeFilters, activePage, activeSort, activeOrder, failed) => {
    setLoading(true)
    setError(null)
    try {
      const data = await getFiles({ ...activeFilters, failed: failed ? 1 : undefined, sort: activeSort, order: activeOrder, page: activePage, per_page: PER_PAGE })
      setFiles(data.items)
      setTotal(data.total)
      setPages(data.pages)
    } catch (e) {
      setError(e.message)
    } finally {
      setLoading(false)
      setLoadedOnce(true)
    }
  }, [])

  const fetchStats = useCallback(async () => {
    try {
      setStats(await getStats())
    } catch {
      // non-fatal
    }
  }, [])

  useEffect(() => { fetchStats() }, [fetchStats])

  useEffect(() => {
    fetchFiles(filters, page, sort, order, failedView)
  }, [fetchFiles, filters, page, sort, order, failedView])

  useEffect(() => { scrollRef.current?.scrollTo({ top: 0 }) }, [page, filters, sort, order, failedView])

  const handleApplyFilters = (newFilters) => {
    setFilters(newFilters)
    setPage(1)
  }

  const filtersActive = Object.entries(filters)
    .some(([k, v]) => v !== '' && v != null && !(k === 'imagetyp' && v === 'Light Frame'))
  const catalogEmpty = stats && stats.total === 0

  return (
    <div className="page">
      <PageHeader
        title="Catalog"
        subtitle={stats ? `${stats.total.toLocaleString()} files indexed` : 'Browse, screen and grade indexed frames'}>
        {stats?.failed > 0 && (
          <button className={`badge badge-button ${failedView ? 'badge-accent' : 'badge-danger'}`}
                  onClick={() => { setFailedView(v => !v); setPage(1) }}
                  title="Files that couldn't be read (corrupt or truncated)">
            {failedView
              ? <><Icon name="chevronLeft" size={12} />Back to catalog</>
              : <><Icon name="alert" size={12} />{stats.failed.toLocaleString()} failed to index</>}
          </button>
        )}
      </PageHeader>

      <div className="catalog-layout">
        <FilterPanel stats={stats} onApply={handleApplyFilters} />

        <div className="grid-area">
          <div className="grid-toolbar">
            <div className="grid-count">
              {failedView && <span className="badge badge-danger">Failed files</span>}
              {loadedOnce && <span><strong>{total.toLocaleString()}</strong> {total === 1 ? 'frame' : 'frames'}</span>}
              {loading && <Spinner size={14} />}
            </div>
            <div className="grid-sort">
              <label htmlFor="catalog-sort">Sort by</label>
              <select id="catalog-sort" value={sort} onChange={e => { setSort(e.target.value); setPage(1) }}>
                {SORTS.map(([value, label]) => <option key={value} value={value}>{label}</option>)}
              </select>
              <button className="btn-secondary btn-square"
                      onClick={() => { setOrder(o => (o === 'desc' ? 'asc' : 'desc')); setPage(1) }}
                      title={order === 'desc' ? 'Descending — click for ascending' : 'Ascending — click for descending'}
                      aria-label={order === 'desc' ? 'Sort descending' : 'Sort ascending'}>
                <Icon name={order === 'desc' ? 'arrowDown' : 'arrowUp'} size={15} />
              </button>
            </div>
          </div>

          <div className="grid-scroll" ref={scrollRef}>
            {error && <Alert>Couldn't load frames: {errorText(error)}</Alert>}

            {!loadedOnce ? (
              <div className="file-grid"><CardSkeletons count={15} /></div>
            ) : files.length === 0 ? (
              loading ? <LoadingBlock>Loading frames…</LoadingBlock>
                : failedView ? (
                  <EmptyState icon="check" title="No failed files">Every indexed file was read successfully.</EmptyState>
                ) : catalogEmpty ? (
                  <EmptyState icon="database" title="Your catalog is empty"
                              action={<button className="btn-primary" onClick={() => { window.location.hash = '/settings' }}>
                                <Icon name="folder" size={15} />Add folders in Settings
                              </button>}>
                    Add the folders that hold your FITS / XISF files, then run a scan to index them.
                  </EmptyState>
                ) : (
                  <EmptyState icon="search" title="No frames match">
                    {filtersActive ? 'Try loosening or resetting the filters.' : 'Nothing here yet — run a scan from Settings.'}
                  </EmptyState>
                )
            ) : (
              <>
                <div className={`file-grid${loading ? ' is-stale' : ''}`}>
                  {files.map(f => (
                    <FileCard key={f.id} file={f} onClick={grading.open}
                              onCompare={compare.toggle} inCompare={compare.has(f.id)} />
                  ))}
                </div>
                <Pagination page={page} pages={pages} total={total} perPage={PER_PAGE}
                            onPage={setPage} disabled={loading} />
              </>
            )}
          </div>
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
