// What a Services row's start, stop or restart waits for. Each row has its own action state:
//
//   idle -> requesting   the POST is in flight
//        -> confirming   the POST answered (or timed out, see outcomeUnknown); the table is re-read
//                        until the row shows the action's target state
//        -> idle         confirmed: only now is it reported as done
//   requesting -> failed the POST was refused: its reason is shown, the row can act again
//   confirming -> failed no target state within GIVE_UP_MS
//
// The page (ServicesPage.jsx) holds the states; the rules for "confirmed" live here.

// While confirming, the table is read every CONFIRM_FAST_MS for the first CONFIRM_FAST_FOR_MS,
// then every CONFIRM_SLOW_MS, until GIVE_UP_MS. The lifecycle proxy itself waits 180 s
// (_LIFECYCLE_TIMEOUT in dashboard/app.py), so a slow action gets as long to show up here.
export const CONFIRM_FAST_MS = 2000
export const CONFIRM_FAST_FOR_MS = 60000
export const CONFIRM_SLOW_MS = 5000
export const GIVE_UP_MS = 180000

const RUNNING = new Set(['up', 'starting', 'unhealthy'])
const STOPPED = new Set(['stopped', 'done'])

// A failure that does not say whether the action happened: the network dropped, or the proxy
// gave up waiting (503 from the dashboard's own timeout, 504 from the edge). The action may still
// be running, so the table decides.
export function outcomeUnknown(error) {
  return [0, 503, 504].includes(error?.status)
}

const UNIT_SECONDS = { second: 1, minute: 60, hour: 3600, day: 86400, week: 604800, month: 2592000, year: 31536000 }

// Docker's "Up ..." duration (its HumanDuration: "Less than a second", "5 seconds", "About a
// minute", "3 hours", ...) as rough seconds, or null for text it does not recognise.
export function uptimeSeconds(uptime) {
  const text = (uptime || '').trim().toLowerCase()
  if (text === 'less than a second') return 0
  const match = /^(?:about )?(an?|\d+) (second|minute|hour|day|week|month|year)s?$/.exec(text)
  if (!match) return null
  const count = /^\d+$/.test(match[1]) ? Number(match[1]) : 1
  return count * UNIT_SECONDS[match[2]]
}

// Whether `row` (from a fresh table read) shows that `action` on `before` (the row as it was when
// the action was asked for) has taken effect.
export function isConfirmed(action, before, row) {
  if (!row) return false
  if (action === 'stop') return STOPPED.has(row.verdict)
  if (!RUNNING.has(row.verdict)) return false
  if (action === 'start') return true
  // A restart: the container's start time moved, when the row carries one.
  if (before.started_at != null && row.started_at != null) return row.started_at !== before.started_at
  // Otherwise its uptime went backwards; text that cannot be compared must at least have changed.
  const was = uptimeSeconds(before.uptime)
  const now = uptimeSeconds(row.uptime)
  if (was == null || now == null) return row.status !== before.status
  return now < was
}
