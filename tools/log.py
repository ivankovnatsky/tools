class Color:
    GREEN = "\033[32m"
    RED = "\033[31m"
    YELLOW = "\033[33m"
    BLUE = "\033[34m"
    RESET = "\033[0m"


_verbose = False


def set_verbose(enabled: bool):
    global _verbose
    _verbose = enabled


def log(message: str, color: str = ""):
    print(f"{color}{message}{Color.RESET}")


def debug(message: str, color: str = ""):
    if _verbose:
        print(f"{color}{message}{Color.RESET}")
