// The Services logs drawer's reader: one buffer per container, filled from its logs proxy.
//
//   first read   ?tail=INITIAL_TAIL                 the newest lines; the answer's last_ts is the cursor
//   every 3 s    ?tail=FOLLOW_TAIL&since=<cursor>   only lines after the cursor (since is exclusive in
//                                                   ops-controller), appended; no client dedupe
//
// An ops-controller without the cursor (before ordo/control/logs.py) answers no last_ts and
// ignores since, so the reader never gets a cursor: every read is a first read whose answer
// replaces the buffer, the whole tail again, never a repeated line.
//
// The buffer keeps the newest BUFFER_MAX lines. `dropped` counts what the cap removed, `truncated`
// says ops-controller cut an answer (its byte cap) or a read may have skipped lines.
import { useEffect, useState } from 'react'
import { api, pollWhileVisible } from '../api.js'

export const INITIAL_TAIL = 300
export const FOLLOW_TAIL = 1000
export const POLL_MS = 3000
export const BUFFER_MAX = 1000

// `received` counts the lines known to be new and `changes` every answer that changed the buffer,
// so the drawer can say how much arrived while the reader was scrolled up.
export const EMPTY_BUFFER = Object.freeze({
  lines: /** @type {string[]} */ ([]), cursor: null, lastTs: null, dropped: 0, truncated: false, received: 0, changes: 0,
})

export function logsUrl(basePath, cursor) {
  const query = cursor
    ? new URLSearchParams({ tail: String(FOLLOW_TAIL), since: cursor })
    : new URLSearchParams({ tail: String(INITIAL_TAIL) })
  return `${basePath}/logs?${query}`
}

function splitLines(text) {
  if (!text) return []
  const lines = text.split('\n')
  if (lines[lines.length - 1] === '') lines.pop() // the newline that ends the last line
  return lines
}

// The buffer after one answer. Pure, so the rules above are tested without a page.
export function applyAnswer(buffer, answer) {
  const lines = splitLines(answer?.logs)
  const cursor = answer?.last_ts || buffer.cursor
  const lastTs = answer?.last_ts || buffer.lastTs

  if (!buffer.cursor) {
    // A first read replaces the buffer. Against an old ops-controller every read is one: the same
    // tail again adds nothing, a different one is new text of an unknown line count.
    const kept = lines.slice(-BUFFER_MAX)
    const same = kept.length === buffer.lines.length && kept.every((line, i) => line === buffer.lines[i])
    const added = same ? 0 : buffer.lines.length ? null : kept.length
    return {
      lines: kept, cursor, lastTs, dropped: 0, truncated: Boolean(answer?.truncated),
      received: buffer.received + (added || 0), changes: buffer.changes + (same ? 0 : 1),
    }
  }

  if (!lines.length) return { ...buffer, cursor, lastTs, truncated: buffer.truncated || Boolean(answer?.truncated) }
  const all = buffer.lines.concat(lines)
  const over = Math.max(0, all.length - BUFFER_MAX)
  return {
    lines: over ? all.slice(over) : all,
    cursor,
    lastTs,
    dropped: buffer.dropped + over,
    // A read that came back full may have had more lines after the cursor than it could return.
    truncated: buffer.truncated || Boolean(answer?.truncated) || lines.length >= FOLLOW_TAIL,
    received: buffer.received + lines.length,
    changes: buffer.changes + 1,
  }
}

// What the buffer holds, as the drawer labels it: "300 lines, newest at 04:10:21".
export function describeBuffer(buffer, now = Date.now()) {
  const count = `${buffer.lines.length.toLocaleString()} ${buffer.lines.length === 1 ? 'line' : 'lines'}`
  return buffer.lastTs ? `${count}, newest at ${formatLogTime(buffer.lastTs, now)}` : count
}

// A docker timestamp (nanoseconds, UTC) as local time to the second, with the date when it is not
// today. JavaScript dates stop at milliseconds, so the rest of the fraction is dropped first.
export function formatLogTime(timestamp, now = Date.now()) {
  const when = new Date(timestamp.replace(/(\.\d{3})\d+/, '$1'))
  if (Number.isNaN(when.getTime())) return timestamp
  const time = when.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit', second: '2-digit', hour12: false })
  if (when.toDateString() === new Date(now).toDateString()) return time
  return `${when.toLocaleDateString([], { month: 'short', day: 'numeric' })} ${time}`
}

// What one notice line says about lines the drawer is not showing, or null.
export function bufferNotice(buffer) {
  const notes = []
  if (buffer.truncated) notes.push('Some lines are missing: an answer was larger than the control plane sends at once.')
  if (buffer.dropped) {
    notes.push(`Showing the newest ${BUFFER_MAX.toLocaleString()} lines; ${buffer.dropped.toLocaleString()} older were dropped.`)
  }
  return notes.length ? notes.join(' ') : null
}

export function bufferText(buffer) {
  return buffer.lines.length ? buffer.lines.join('\n') + '\n' : ''
}

// The buffer for `basePath` (a row's ops route), read now and every POLL_MS while the tab is
// visible. A new basePath aborts the read in flight and starts from an empty buffer. `loaded` is
// whether any read has answered; `error` is the last read's failure (the buffer is kept).
export function useLogTail(basePath) {
  const [buffer, setBuffer] = useState(EMPTY_BUFFER)
  const [error, setError] = useState(null)
  const [loaded, setLoaded] = useState(false)

  useEffect(() => {
    setBuffer(EMPTY_BUFFER)
    setError(null)
    setLoaded(false)
    if (!basePath) return undefined
    const controller = new AbortController()
    let current = EMPTY_BUFFER
    let reading = null
    const read = () => {
      // One read at a time: two reads from the same cursor would append the same lines twice.
      if (reading) return reading
      reading = (async () => {
        try {
          const answer = await api.get(logsUrl(basePath, current.cursor), { signal: controller.signal })
          if (controller.signal.aborted) return
          current = applyAnswer(current, answer)
          setBuffer(current)
          setError(null)
          setLoaded(true)
        } catch (e) {
          if (e.name !== 'AbortError' && !controller.signal.aborted) setError(e)
        } finally {
          reading = null
        }
      })()
      return reading
    }
    const stop = pollWhileVisible(read, POLL_MS)
    return () => {
      stop()
      controller.abort()
    }
  }, [basePath])

  return { buffer, error, loaded }
}
