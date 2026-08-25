#!/usr/bin/env python3
"""Minimal GUI for the Treasure macro: position the Roblox window, then
start/stop the dig loop. Everything else (the loop itself, hotkeys) lives in
prospector_engine/engine.py -- this is just a thin front end for it."""
import queue
import threading
import tkinter as tk

from prospector_engine import engine

_status_q = queue.Queue()


def _on_status(msg):
    _status_q.put(msg)


class App:
    def __init__(self, root):
        self.root = root
        root.title("Treasure")
        root.resizable(False, False)

        self.window_status = tk.StringVar(value="Window not positioned yet.")
        self.run_status = tk.StringVar(value="Stopped")

        tk.Label(root, text="Treasure", font=("Helvetica", 16, "bold")).pack(
            pady=(14, 6))

        tk.Button(root, text="Find & Position Roblox Window", width=32,
                  command=self.pin_window).pack(pady=(0, 4), padx=14)
        tk.Label(root, textvariable=self.window_status, wraplength=280,
                 justify="left", fg="#666").pack(padx=14, pady=(0, 10))

        btnrow = tk.Frame(root)
        btnrow.pack(pady=4)
        tk.Button(btnrow, text="Start", width=10,
                  command=self.start).grid(row=0, column=0, padx=4)
        tk.Button(btnrow, text="Stop", width=10,
                  command=self.stop).grid(row=0, column=1, padx=4)

        tk.Label(root, textvariable=self.run_status,
                 font=("Helvetica", 11, "bold")).pack(pady=(10, 2))
        tk.Label(root, text="In-game: F1 find+pin+start, F2 stop, "
                             "F3 pixel/color popup, Esc quit.",
                 fg="#888").pack(pady=(0, 14))

        threading.Thread(target=engine.run, kwargs={"on_status": _on_status},
                          daemon=True).start()
        listener = engine.make_listener()
        listener.start()

        self.root.after(100, self._poll)

    def pin_window(self):
        ok, msg = engine.pin_window(engine.WINDOW_W, engine.WINDOW_H)
        self.window_status.set(msg)
        if ok:
            engine.CALIB_WINDOW_ORIGIN = [0, 0]

    def start(self):
        engine.request_start("gui")
        self.window_status.set("Window pinned, loop started." if engine.State.running
                                else "Couldn't pin window -- see run status below.")

    def stop(self):
        engine.request_stop("gui")

    def _poll(self):
        try:
            while True:
                msg = _status_q.get_nowait()
                self.run_status.set(msg)
        except queue.Empty:
            pass
        self.root.after(100, self._poll)

    def on_close(self):
        engine.request_quit("gui")
        self.root.destroy()


def main():
    root = tk.Tk()
    app = App(root)
    root.protocol("WM_DELETE_WINDOW", app.on_close)
    root.mainloop()


if __name__ == "__main__":
    main()
