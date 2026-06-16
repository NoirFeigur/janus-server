from collections.abc import Callable
from typing import Any

TaskFn = Callable[..., Any]

TASKS: list[TaskFn] = []
