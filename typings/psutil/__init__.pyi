def cpu_percent(interval: float | None = None, percpu: bool = False) -> float: ...

class svmem:
    total: int
    available: int
    percent: float
    used: int
    free: int

def virtual_memory() -> svmem: ...

class sdiskusage:
    total: int
    used: int
    free: int
    percent: float

def disk_usage(path: str) -> sdiskusage: ...
def cpu_count(logical: bool = True) -> int: ...
