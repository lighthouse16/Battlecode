"""Executable Crash Bot Fixture."""

import sys


def main():
    sys.stderr.write("Simulating unhandled bot crash!\n")
    sys.stderr.flush()
    raise RuntimeError("Controlled fixture crash triggered!")


if __name__ == "__main__":
    main()
