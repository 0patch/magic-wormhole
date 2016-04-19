from __future__ import print_function
import os, sys
from wormhole.errors import TransferError
from wormhole.timing import DebugTiming
from .cli_args import parser

def dispatch(args):
    if args.func == "send/send":
        from . import cmd_send
        return cmd_send.send_twisted_sync(args)
    if args.func == "receive/receive":
        _start = args.timing.add_event("import c_r_t")
        from . import cmd_receive
        args.timing.finish_event(_start)
        return cmd_receive.receive_twisted_sync(args)

    raise ValueError("unknown args.func %s" % args.func)

def run(args, cwd, stdout, stderr, executable=None):
    """This is invoked directly by the 'wormhole' entry-point script. It can
    also invoked by entry() below."""

    args = parser.parse_args()
    if not getattr(args, "func", None):
        # So far this only works on py3. py2 exits with a really terse
        # "error: too few arguments" during parse_args().
        parser.print_help()
        sys.exit(0)
    args.cwd = cwd
    args.stdout = stdout
    args.stderr = stderr
    args.timing = timing = DebugTiming()

    try:
        timing.add_event("command dispatch")
        rc = dispatch(args)
        timing.add_event("exit")
        if args.dump_timing:
            timing.write(args.dump_timing, stderr)
        return rc
    except TransferError as e:
        print(e, file=stderr)
        if args.dump_timing:
            timing.write(args.dump_timing, stderr)
        return 1
    except ImportError as e:
        print("--- ImportError ---", file=stderr)
        print(e, file=stderr)
        print("Please run 'python setup.py build'", file=stderr)
        raise
        return 1

def entry():
    """This is used by a setuptools entry_point. When invoked this way,
    setuptools has already put the installed package on sys.path ."""
    return run(sys.argv[1:], os.getcwd(), sys.stdout, sys.stderr,
               executable=sys.argv[0])

if __name__ == "__main__":
    args = parser.parse_args()
    print(args)