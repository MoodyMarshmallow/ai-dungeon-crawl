import os
import resource
import signal
import sys

resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
resource.setrlimit(resource.RLIMIT_NOFILE, (64, 64))
resource.setrlimit(resource.RLIMIT_FSIZE, (16 * 1024 * 1024,) * 2)
resource.setrlimit(resource.RLIMIT_CPU, (int(sys.argv[3]),) * 2)
resource.setrlimit(resource.RLIMIT_NPROC, (int(sys.argv[2]),) * 2)
# Restore the default so bash pipelines terminate quietly when their reader exits.
signal.signal(signal.SIGPIPE, signal.SIG_DFL)
os.execv('/bin/bash', ['bash', '--noprofile', '--norc', '-c', sys.argv[1]])
