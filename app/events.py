"""Bus de eventos thread-safe entre los callbacks de PJSUA2 y la loop asyncio.

Los callbacks de pjsua2 corren en threads propios de PJSIP. NUNCA deben tocar
objetos asyncio directamente. En su lugar publican eventos con `emit()` (síncrono,
thread-safe), y la loop de FastAPI los consume vía `subscribe()` para difundirlos
por WebSocket.
"""
import asyncio
import queue
import threading
from typing import Any, Dict, List, Optional


class EventBus:
    def __init__(self) -> None:
        self._q: "queue.Queue[Dict[str, Any]]" = queue.Queue()
        self._subscribers: List[asyncio.Queue] = []
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._lock = threading.Lock()
        self._pump_task: Optional[asyncio.Task] = None

    # ---- lado productor (threads de pjsua2, síncrono) ----
    def emit(self, type_: str, **data: Any) -> None:
        evt = {"type": type_, **data}
        self._q.put(evt)

    # ---- lado consumidor (asyncio / FastAPI) ----
    def attach_loop(self, loop: asyncio.AbstractEventLoop) -> None:
        self._loop = loop
        self._pump_task = loop.create_task(self._pump())

    async def _pump(self) -> None:
        """Drena la cola thread-safe y reparte a los subscribers asyncio."""
        while True:
            evt = await asyncio.get_event_loop().run_in_executor(None, self._q.get)
            with self._lock:
                subs = list(self._subscribers)
            for sub in subs:
                try:
                    sub.put_nowait(evt)
                except asyncio.QueueFull:
                    pass

    def subscribe(self) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue(maxsize=1000)
        with self._lock:
            self._subscribers.append(q)
        return q

    def unsubscribe(self, q: asyncio.Queue) -> None:
        with self._lock:
            if q in self._subscribers:
                self._subscribers.remove(q)


# Instancia global compartida.
bus = EventBus()
