"""A tiny publish/subscribe bus.

Workers emit from any thread; when a GLib main loop drives the app, callbacks
always run on the main thread. Without a main loop (the headless refresh
command) callbacks run synchronously in the emitting thread.

Topics in use:
  feeds-changed       (feed_ids: set | None)
  episodes-changed    (episode_ids: set | None)
  queue-changed       ()
  downloads-changed   ()
  download-progress   (episode_id, received, total)
  refresh-state       (running: bool, done: int, total: int)
  player-state        (state: dict)
  sync-state          (running: bool, status: str)
  message             (text: str, kind: str)
"""

import threading
import traceback
from collections import defaultdict


class EventBus:
    def __init__(self):
        self._subscribers = defaultdict(list)
        self._lock = threading.Lock()
        self._glib = None

    def attach_main_loop(self):
        from gi.repository import GLib

        self._glib = GLib

    def subscribe(self, topic, callback):
        with self._lock:
            self._subscribers[topic].append(callback)
        return (topic, callback)

    def unsubscribe(self, token):
        topic, callback = token
        with self._lock:
            if callback in self._subscribers.get(topic, []):
                self._subscribers[topic].remove(callback)

    def emit(self, topic, *args):
        if self._glib is not None and threading.current_thread() is not threading.main_thread():
            self._glib.idle_add(self._dispatch, topic, args)
        else:
            self._dispatch(topic, args)

    def _dispatch(self, topic, args):
        with self._lock:
            callbacks = list(self._subscribers.get(topic, []))
        for callback in callbacks:
            try:
                callback(*args)
            except Exception:
                traceback.print_exc()
        return False


bus = EventBus()
