import { useState, useEffect, useCallback } from 'react'
import { getQueries, saveQuery, deleteQuery, errorText } from './api.js'
import Icon from './Icon.jsx'
import { Modal, useConfirm, useToast } from './ui.jsx'

// Build a nested folder tree from queries' "/"-separated folder paths
function buildTree(queries) {
  const root = { name: '', folders: {}, queries: [] }
  for (const q of queries) {
    const parts = (q.folder || '').split('/').filter(Boolean)
    let node = root
    for (const p of parts) {
      node.folders[p] = node.folders[p] || { name: p, folders: {}, queries: [] }
      node = node.folders[p]
    }
    node.queries.push(q)
  }
  return root
}

function FolderNode({ node, depth, onLoad, onDelete }) {
  const [open, setOpen] = useState(true)
  const subfolders = Object.values(node.folders).sort((a, b) => a.name.localeCompare(b.name))
  return (
    <div>
      {node.name && (
        <button className="sq-folder" style={{ paddingLeft: 6 + (depth - 1) * 14 }} onClick={() => setOpen(o => !o)}
                aria-expanded={open}>
          <Icon name={open ? 'chevronDown' : 'chevronRight'} size={13} />
          <Icon name="folder" size={14} />
          {node.name}
        </button>
      )}
      {open && (
        <>
          {subfolders.map(f => (
            <FolderNode key={f.name} node={f} depth={depth + 1} onLoad={onLoad} onDelete={onDelete} />
          ))}
          {[...node.queries].sort((a, b) => a.name.localeCompare(b.name)).map(q => (
            <div key={q.id} className="sq-item" style={{ paddingLeft: depth * 14 }}>
              <button className="sq-load" onClick={() => onLoad(q)} title="Load and run this query">
                <Icon name="search" size={13} /><span>{q.name}</span>
              </button>
              <button className="icon-btn icon-btn-sm danger" onClick={() => onDelete(q)}
                      title="Delete" aria-label={`Delete ${q.name}`}>
                <Icon name="trash" size={13} />
              </button>
            </div>
          ))}
        </>
      )}
    </div>
  )
}

export default function SavedQueries({ currentState, onLoad }) {
  const [queries, setQueries] = useState([])
  const [dialog, setDialog] = useState(false)
  const [form, setForm] = useState({ name: '', folder: '' })
  const [busy, setBusy] = useState(false)
  const toast = useToast()
  const confirm = useConfirm()

  const refresh = useCallback(() => { getQueries().then(setQueries).catch(() => {}) }, [])
  useEffect(() => { refresh() }, [refresh])

  const folders = [...new Set(queries.map(q => q.folder).filter(Boolean))].sort()

  const handleSave = async (e) => {
    e.preventDefault()
    const name = form.name.trim()
    if (!name) return
    setBusy(true)
    try {
      await saveQuery({ name, folder: form.folder.trim(), query: currentState() })
      toast(`Saved “${name}”`, { tone: 'success' })
      setDialog(false)
      refresh()
    } catch (err) {
      toast(`Couldn't save: ${errorText(err)}`, { tone: 'danger' })
    } finally {
      setBusy(false)
    }
  }

  const handleDelete = async (q) => {
    const ok = await confirm({
      title: 'Delete saved query?',
      message: `“${q.name}” will be removed. The frames it matched are not affected.`,
      confirmLabel: 'Delete',
      tone: 'danger',
    })
    if (!ok) return
    try { await deleteQuery(q.id); refresh() } catch (err) { toast(errorText(err), { tone: 'danger' }) }
  }

  const tree = buildTree(queries)
  return (
    <>
      <details className="card disclosure saved-queries">
        <summary>
          <Icon name="chevronDown" size={14} />
          <Icon name="bookmark" size={14} />
          Saved queries
          <span className="badge">{queries.length}</span>
        </summary>
        <div className="sq-body">
          <div className="row">
            <button className="btn-secondary btn-sm" onClick={() => { setForm({ name: '', folder: '' }); setDialog(true) }}>
              <Icon name="plus" size={14} />Save current query
            </button>
          </div>
          {queries.length === 0
            ? <p className="help-text">No saved queries yet. Save the current scope and expression to reuse them later.</p>
            : <div className="sq-tree"><FolderNode node={tree} depth={0} onLoad={onLoad} onDelete={handleDelete} /></div>}
        </div>
      </details>

      {dialog && (
        <Modal title="Save query" subtitle="Stores the scope, the expression and the reference-frame choice."
               onClose={() => setDialog(false)}
               footer={<>
                 <button className="btn-secondary" onClick={() => setDialog(false)}>Cancel</button>
                 <button className="btn-primary" type="submit" form="save-query-form" disabled={busy || !form.name.trim()}>
                   {busy ? 'Saving…' : 'Save query'}
                 </button>
               </>}>
          <form id="save-query-form" className="stack-sm" onSubmit={handleSave}>
            <div className="field">
              <label htmlFor="sq-name">Name</label>
              <input id="sq-name" type="text" placeholder="e.g. Ha — good seeing" value={form.name}
                     onChange={e => setForm({ ...form, name: e.target.value })} />
            </div>
            <div className="field">
              <label htmlFor="sq-folder">Folder <span className="text-muted">(optional)</span></label>
              <input id="sq-folder" type="text" list="sq-folders" placeholder="e.g. Mono/NGC 7000" value={form.folder}
                     onChange={e => setForm({ ...form, folder: e.target.value })} />
              <datalist id="sq-folders">{folders.map(f => <option key={f} value={f} />)}</datalist>
              <span className="field-hint">Use / to nest folders.</span>
            </div>
          </form>
        </Modal>
      )}
    </>
  )
}
