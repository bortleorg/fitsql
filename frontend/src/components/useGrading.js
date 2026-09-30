import { useCallback, useRef, useState } from 'react'
import { setGrade } from './api.js'

// `rejected` column: 1 = rejected, 0 = accepted, null = unmarked
export const gradeOf = (rejected) => (rejected === 1 ? 'rejected' : rejected === 0 ? 'accepted' : null)

/**
 * Grading state for one page of frames: which frame is open, prev/next,
 * accept / reject / unmark (optionally advancing), and undo/redo of grades.
 * `setItems(fn)` must apply `fn` to the page's item list.
 */
export default function useGrading(items, setItems) {
  const [openId, setOpenId] = useState(null)
  const [done, setDone] = useState([])      // undo stack of { id, from, to }
  const [undone, setUndone] = useState([])  // redo stack
  const [error, setError] = useState(null)
  const busy = useRef(false)                // one write at a time (held keys)

  const index = openId == null ? -1 : items.findIndex(f => f.id === openId)
  const current = index >= 0 ? items[index] : null

  const move = useCallback((delta) => {
    if (index < 0) return
    const next = items[Math.min(items.length - 1, Math.max(0, index + delta))]
    if (next) setOpenId(next.id)
  }, [items, index])

  const write = useCallback(async (id, grade) => {
    if (busy.current) return false
    busy.current = true
    setError(null)
    try {
      const r = await setGrade(id, grade)
      setItems(list => list.map(f => (f.id === id ? { ...f, rejected: r.rejected } : f)))
      return true
    } catch (e) {
      setError(e.message)
      return false
    } finally {
      busy.current = false
    }
  }, [setItems])

  const grade = useCallback(async (to, { advance = true } = {}) => {
    if (!current) return
    const from = gradeOf(current.rejected)
    if (from !== to) {
      if (!(await write(current.id, to))) return
      setDone(s => [...s, { id: current.id, from, to }])
      setUndone([])
    }
    if (advance) move(1)
  }, [current, write, move])

  // Undo/redo jump back to the frame they touch, so the change is visible
  const undo = useCallback(async () => {
    const op = done[done.length - 1]
    if (!op || !(await write(op.id, op.from))) return
    setDone(s => s.slice(0, -1))
    setUndone(s => [...s, op])
    if (items.some(f => f.id === op.id)) setOpenId(op.id)
  }, [done, write, items])

  const redo = useCallback(async () => {
    const op = undone[undone.length - 1]
    if (!op || !(await write(op.id, op.to))) return
    setUndone(s => s.slice(0, -1))
    setDone(s => [...s, op])
    if (items.some(f => f.id === op.id)) setOpenId(op.id)
  }, [undone, write, items])

  return {
    current, index, count: items.length, error,
    open: (file) => setOpenId(file.id),
    close: () => setOpenId(null),
    move, grade, undo, redo,
    canUndo: done.length > 0, canRedo: undone.length > 0,
  }
}
