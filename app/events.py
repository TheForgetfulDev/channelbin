"""In-process SSE pub/sub. Watchdog threads publish; Flask SSE routes subscribe."""
import json
import queue
import threading

_lock = threading.Lock()
# Maps recording_id (int) or 'all' to list of subscriber queues.
_subscribers: dict = {}


def subscribe(recording_id=None) -> queue.Queue:
    """Return a new Queue subscribed to events for recording_id (or all if None)."""
    q = queue.Queue(maxsize=200)
    key = recording_id if recording_id is not None else 'all'
    with _lock:
        _subscribers.setdefault(key, []).append(q)
    return q


def unsubscribe(q: queue.Queue, recording_id=None):
    key = recording_id if recording_id is not None else 'all'
    with _lock:
        lst = _subscribers.get(key, [])
        if q in lst:
            lst.remove(q)


def publish(recording_id: int, event_type: str, data: dict):
    """Publish an event. Called from watchdog/recorder threads."""
    payload = json.dumps({
        'recording_id': recording_id,
        'event': event_type,
        'data': data,
    })
    with _lock:
        targets = list(_subscribers.get(recording_id, [])) + list(_subscribers.get('all', []))
    for q in targets:
        try:
            q.put_nowait(payload)
        except queue.Full:
            pass  # slow consumer - drop rather than block
