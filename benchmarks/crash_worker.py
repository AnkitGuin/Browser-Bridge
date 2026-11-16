"""Internal harness process used to simulate abrupt worker death."""

import os
import sys

from bridge import runstore, workflow


def exit_after_submit(*_):
    os._exit(73)


if __name__ == "__main__":
    workflow.execute(*runstore.claim(sys.argv[1]), after_submit=exit_after_submit)
