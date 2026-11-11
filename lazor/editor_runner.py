"""Run editor simulations in an isolated, cancellable child process.

Importing this module starts no process and does not require a display.
The GUI owns one EditorRunner and calls poll from its normal event loop.
"""

import multiprocessing as mp
import pickle
from math import isfinite
from queue import Empty

from .bff import parse_bff
from .simulation import simulate
from .solver import solve_constraints


def _run_worker(mode, text, timeout, results):
    """Compute one immutable input snapshot and send one complete message."""
    try:
        puzzle = parse_bff(text, source="editor")
        if mode == "simulate":
            value = simulate(puzzle)
        else:
            value = solve_constraints(puzzle, timeout=timeout)
        # Serialize here, not in Queue's background feeder. Serialization
        # failures can then be returned as errors instead of disappearing.
        payload = pickle.dumps((mode, value))
    except Exception as exc:
        payload = pickle.dumps(("error", f"{type(exc).__name__}: {exc}"))

    try:
        results.put(payload)
    finally:
        # Flush the complete payload before a normal worker exit. The parent
        # reads before joining, so large ray traces cannot deadlock the pipe.
        results.close()
        results.join_thread()


class EditorRunner:
    """Own one background operation, with no Tk calls in the worker.

    ``running`` remains true until the result is collected or cancelled,
    even if the child has already finished. Polling returns exactly one
    ('simulate', SimulationResult), ('solve', PlanningResult), or an error.
    Cancellation discards only this runner's task and allows immediate reuse.
    """

    def __init__(self):
        self._context = mp.get_context("spawn")
        self._process = None
        self._results = None

    @property
    def running(self) -> bool:
        """Whether an operation or its uncollected result is still pending."""
        return self._process is not None

    def start(
        self, mode: str, text: str, *, timeout: float = 15.0,
    ) -> None:
        """Start simulation or planning after synchronous argument checks."""
        if self.running:
            raise RuntimeError("An editor operation is already running.")
        if mode not in ("simulate", "solve"):
            raise ValueError("mode must be 'simulate' or 'solve'.")
        if not isinstance(text, str):
            raise ValueError("The board input must be text.")
        if (
            isinstance(timeout, bool)
            or not isinstance(timeout, (int, float))
            or not isfinite(timeout)
            or timeout < 0
        ):
            raise ValueError("timeout must be finite and nonnegative.")

        results = self._context.Queue(maxsize=1)
        process = self._context.Process(
            target=_run_worker,
            args=(mode, text, timeout, results),
            daemon=True,
        )
        try:
            process.start()
        except Exception:
            results.close()
            results.cancel_join_thread()
            process.close()
            raise
        self._results = results
        self._process = process

    def poll(self) -> tuple[str, object] | None:
        """Collect a ready result without waiting for a running computation."""
        if not self.running:
            return None
        try:
            payload = self._results.get_nowait()
        except Empty:
            if self._process.is_alive():
                return None
            # A dead process cannot send more messages. Retry once to drain
            # a just-flushed queue before reporting an abnormal exit.
            try:
                payload = self._results.get(timeout=0.01)
            except Empty:
                code = self._process.exitcode
                self._cleanup()
                return "error", f"Worker exited without result (code {code})."
            except (EOFError, OSError, ValueError) as exc:
                self._cleanup()
                return "error", f"Could not read worker result: {exc}"
        except (EOFError, OSError, ValueError) as exc:
            self._cleanup()
            return "error", f"Could not read worker result: {exc}"

        try:
            message = pickle.loads(payload)
            if (
                not isinstance(message, tuple)
                or len(message) != 2
                or message[0] not in ("simulate", "solve", "error")
            ):
                raise ValueError("Unexpected worker message.")
        except Exception as exc:
            message = ("error", f"Could not decode worker result: {exc}")
        self._cleanup()
        return message

    def cancel(self) -> None:
        """Stop this operation, discard any result, and release its handles."""
        self._cleanup(terminate=True)

    def _cleanup(self, *, terminate: bool = False) -> None:
        """Reap only our own child, using bounded waits on all exit paths."""
        process, results = self._process, self._results
        self._process = self._results = None
        if process is not None:
            if terminate and process.is_alive():
                process.terminate()
            process.join(timeout=0.1)
            if process.is_alive():
                process.terminate()
                process.join(timeout=0.5)
            if process.is_alive():
                process.kill()
                process.join(timeout=0.5)
            if not process.is_alive():
                process.close()
        if results is not None:
            # The parent never puts messages, so it has no feeder to wait on.
            # Cancel may leave a partial child write, which is discarded.
            results.cancel_join_thread()
            results.close()
