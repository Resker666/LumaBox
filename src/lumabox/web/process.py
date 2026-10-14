"""Own the complete downloader process tree, including after its parent exits."""
import os
import signal
import subprocess
import time


class WindowsJob:
    def __init__(self):
        import ctypes as c
        from ctypes import wintypes as w

        class BasicLimit(c.Structure):
            _fields_ = [("process_time", c.c_longlong), ("job_time", c.c_longlong), ("flags", w.DWORD),
                        ("min_working_set", c.c_size_t), ("max_working_set", c.c_size_t),
                        ("active_limit", w.DWORD), ("affinity", c.c_size_t),
                        ("priority", w.DWORD), ("scheduling", w.DWORD)]

        class ExtendedLimit(c.Structure):
            _fields_ = [("basic", BasicLimit), ("io", c.c_ulonglong * 6),
                        ("process_memory", c.c_size_t), ("job_memory", c.c_size_t),
                        ("peak_process_memory", c.c_size_t), ("peak_job_memory", c.c_size_t)]

        class Accounting(c.Structure):
            _fields_ = [("times", c.c_longlong * 4), ("faults", w.DWORD), ("total", w.DWORD),
                        ("active", w.DWORD), ("terminated", w.DWORD)]

        class ThreadEntry(c.Structure):
            _fields_ = [("size", w.DWORD), ("usage", w.DWORD), ("id", w.DWORD), ("owner", w.DWORD),
                        ("base_priority", w.LONG), ("delta_priority", w.LONG), ("flags", w.DWORD)]

        self.c, self.w = c, w
        self.Accounting, self.ThreadEntry = Accounting, ThreadEntry
        self.kernel = c.WinDLL("kernel32", use_last_error=True)
        signatures = {
            "CreateJobObjectW": ([c.c_void_p, w.LPCWSTR], w.HANDLE),
            "SetInformationJobObject": ([w.HANDLE, c.c_int, c.c_void_p, w.DWORD], w.BOOL),
            "AssignProcessToJobObject": ([w.HANDLE, w.HANDLE], w.BOOL),
            "TerminateJobObject": ([w.HANDLE, w.UINT], w.BOOL),
            "QueryInformationJobObject": ([w.HANDLE, c.c_int, c.c_void_p, w.DWORD, c.c_void_p], w.BOOL),
            "CreateToolhelp32Snapshot": ([w.DWORD, w.DWORD], w.HANDLE),
            "Thread32First": ([w.HANDLE, c.POINTER(ThreadEntry)], w.BOOL),
            "Thread32Next": ([w.HANDLE, c.POINTER(ThreadEntry)], w.BOOL),
            "OpenThread": ([w.DWORD, w.BOOL, w.DWORD], w.HANDLE),
            "ResumeThread": ([w.HANDLE], w.DWORD),
            "OpenProcess": ([w.DWORD, w.BOOL, w.DWORD], w.HANDLE),
            "WaitForSingleObject": ([w.HANDLE, w.DWORD], w.DWORD),
            "CloseHandle": ([w.HANDLE], w.BOOL),
        }
        for name, (args, result) in signatures.items():
            function = getattr(self.kernel, name)
            function.argtypes, function.restype = args, result
        self.handle = self.kernel.CreateJobObjectW(None, None)
        if not self.handle:
            raise c.WinError(c.get_last_error())
        limits = ExtendedLimit()
        limits.basic.flags = 0x2000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE; no child breakaway.
        if not self.kernel.SetInformationJobObject(self.handle, 9, c.byref(limits), c.sizeof(limits)):
            self.close()
            raise c.WinError(c.get_last_error())

    def attach_and_resume(self, process):
        c, kernel = self.c, self.kernel
        if not kernel.AssignProcessToJobObject(self.handle, int(process._handle)):
            raise c.WinError(c.get_last_error())
        # Popen closes the initial thread handle. Enumerate its suspended thread
        # through documented Toolhelp APIs, after ownership is established.
        snapshot = kernel.CreateToolhelp32Snapshot(4, 0)
        if snapshot == c.c_void_p(-1).value:
            raise c.WinError(c.get_last_error())
        try:
            entry = self.ThreadEntry()
            entry.size = c.sizeof(entry)
            found = kernel.Thread32First(snapshot, c.byref(entry))
            while found:
                if entry.owner == process.pid:
                    thread = kernel.OpenThread(2, False, entry.id)
                    if not thread:
                        raise c.WinError(c.get_last_error())
                    try:
                        if kernel.ResumeThread(thread) == 0xFFFFFFFF:
                            raise c.WinError(c.get_last_error())
                    finally:
                        kernel.CloseHandle(thread)
                    return
                found = kernel.Thread32Next(snapshot, c.byref(entry))
            raise OSError("Suspended downloader thread was not available")
        finally:
            kernel.CloseHandle(snapshot)

    def terminate_and_wait(self):
        c = self.c
        handles = []
        # Accounting can reach zero just before process objects become signaled.
        # Retain real handles to the job's members, rather than reusing their PIDs.
        capacity = 64
        while True:
            members = c.create_string_buffer(8 + c.sizeof(c.c_size_t) * capacity)
            if self.kernel.QueryInformationJobObject(self.handle, 3, members, c.sizeof(members), None):
                count = c.c_uint32.from_buffer(members, 4).value
                for index in range(count):
                    pid = c.c_size_t.from_buffer(members, 8 + index * c.sizeof(c.c_size_t)).value
                    handle = self.kernel.OpenProcess(0x100000, False, pid)
                    if handle:
                        handles.append(handle)
                break
            if c.get_last_error() != 234 or capacity >= 4096:
                raise c.WinError(c.get_last_error())
            capacity *= 2
        try:
            self._terminate_and_wait(handles)
        finally:
            for handle in handles:
                self.kernel.CloseHandle(handle)

    def _terminate_and_wait(self, handles):
        c = self.c
        if not self.kernel.TerminateJobObject(self.handle, 130):
            raise c.WinError(c.get_last_error())
        deadline = time.monotonic() + 5
        while True:
            accounting = self.Accounting()
            if not self.kernel.QueryInformationJobObject(self.handle, 1, c.byref(accounting), c.sizeof(accounting), None):
                raise c.WinError(c.get_last_error())
            if accounting.active == 0 and all(self.kernel.WaitForSingleObject(handle, 0) == 0 for handle in handles):
                return
            if time.monotonic() >= deadline:
                raise subprocess.TimeoutExpired("downloader process tree", 5)
            time.sleep(.02)

    def close(self):
        if self.handle:
            self.kernel.CloseHandle(self.handle)
            self.handle = None


class ProcessTree:
    def __init__(self, command, stdout):
        self.command, self.stdout = command, stdout
        self.process, self.job = None, None
        self.stopped = False

    def __enter__(self):
        try:
            if os.name == "nt":
                self.job = WindowsJob()
                options = {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP | 0x4}  # CREATE_SUSPENDED
            else:
                options = {"start_new_session": True}
            self.process = subprocess.Popen(self.command, stdin=subprocess.DEVNULL, stdout=self.stdout,
                                            stderr=subprocess.DEVNULL, shell=False, **options)
            if self.job:
                self.job.attach_and_resume(self.process)
            return self
        except BaseException:
            if self.job:
                self.job.close()
            if self.process:
                self.process.kill()
                self.process.wait(timeout=5)
            raise

    def stop(self):
        if self.stopped:
            return
        if self.process.poll() is None:
            try:
                if os.name == "nt":
                    self.process.send_signal(signal.CTRL_BREAK_EVENT)
                else:
                    os.killpg(self.process.pid, signal.SIGINT)
                self.process.wait(timeout=4)
            except (OSError, subprocess.TimeoutExpired):
                pass
        # A finished parent says nothing about remaining descendants.
        if self.job:
            self.job.terminate_and_wait()
        else:
            try:
                os.killpg(self.process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        self.process.wait(timeout=5)
        self.stopped = True

    def __exit__(self, *_):
        try:
            self.stop()
        finally:
            if self.job:
                self.job.close()
