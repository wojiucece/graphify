def module_fn(a: int, b: str = "x") -> bool:
    return True


class UserService:
    def __init__(self, name: str, age: int = 0):
        self.name = name

    def fetch(self, limit: int = 10) -> list:
        def helper(x):
            return x
        return helper(limit)

    @property
    def display_name(self) -> str:
        return self.name

    class Nested:
        def inner(self) -> int:
            return 1


class Empty:
    pass


CONSTANT = 42


class Cjk:
    中文属性 = "值"; def after_cjk(self) -> str: return 中文属性
