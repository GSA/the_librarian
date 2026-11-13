import ctypes, os
_l = None
def lib():
    global _l
    if _l is None:
        _l = ctypes.CDLL("/home/nkirrish/.cache/efz3-scratch/shim/libcount.so")
        for n in ("shim_fsync","shim_fdatasync","shim_pwrite","shim_write"):
            getattr(_l,n).restype = ctypes.c_ulong
    return _l
def counts():
    l = lib()
    return {"fsync": l.shim_fsync(), "fdatasync": l.shim_fdatasync(), "pwrite": l.shim_pwrite(), "write": l.shim_write()}
def log_open(path):
    l = lib(); l.shim_log_open.argtypes = [ctypes.c_char_p]; l.shim_log_open(path.encode() if path else None)
def log_close():
    lib().shim_log_close()
