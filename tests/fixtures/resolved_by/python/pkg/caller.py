from .callee import helper_fn


def run_imported():
    return helper_fn()


def run_unimported():
    return unimported_fn()


def run_missing():
    return no_such_fn()
