// Wait for the entire snapshot before scheduling another read, including after errors.
export function createProjectPoller({ read, onSnapshot, onError, interval = 2000,
  setTimer = setTimeout, clearTimer = clearTimeout }) {
  let timer = null
  let controller = null
  let running = false
  let generation = 0
  let failures = 0

  function stop() {
    running = false
    generation += 1
    if (timer !== null) clearTimer(timer)
    timer = null
    controller?.abort()
    controller = null
  }

  async function tick(token) {
    if (!running || token !== generation) return
    timer = null
    controller = new AbortController()
    let keepPolling = true
    try {
      const snapshot = await read(controller.signal)
      if (!running || token !== generation) return
      failures = 0
      keepPolling = onSnapshot(snapshot) !== false
    } catch (error) {
      if (!running || token !== generation) return
      failures += 1
      keepPolling = onError(error, failures) !== false
    } finally {
      if (token === generation) controller = null
    }
    if (!running || token !== generation) return
    if (!keepPolling) {
      stop()
      return
    }
    const delay = Math.min(interval * 2 ** Math.min(failures, 4), 30000)
    timer = setTimer(() => tick(token), delay)
  }

  function start() {
    if (running) return
    running = true
    failures = 0
    const token = ++generation
    timer = setTimer(() => tick(token), 0)
  }

  return { start, stop }
}
