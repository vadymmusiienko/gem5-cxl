"""
This gem5 configuation script creates a simple board to run an X86
"microtest" benchmark binary. (Array of pointers)

This setup is close to the simplest setup possible using the gem5
library. It does not contain any kind of caching, IO, or any non-essential
components. It uses a single local DDR5 memory system.

Usage
-----

```
scons build/ALL/gem5.opt
./build/ALL/gem5.opt configs/example/gem5_library/x86-pointer-arr.py
```
"""

import argparse
import base64
import os
from pathlib import Path
from socket import gethostname

import m5
from gem5.components.boards.simple_board import SimpleBoard
from gem5.components.boards.x86_board import X86Board
from gem5.components.cachehierarchies.classic.no_cache import NoCache

# --- Memory imports ---
from gem5.components.memory import DIMM_DDR5_8400
from gem5.components.processors.cpu_types import CPUTypes
from gem5.components.processors.simple_processor import SimpleProcessor
from gem5.components.processors.simple_switchable_processor import (
    SimpleSwitchableProcessor,
)
from gem5.isas import ISA
from gem5.resources.resource import (
    BinaryResource,
    DiskImageResource,
    KernelResource,
)
from gem5.simulate.exit_event import ExitEvent
from gem5.simulate.simulator import Simulator
from gem5.utils.requires import requires

# This check ensures the gem5 binary contains the X86 ISA target. If not, an
# exception will be thrown.
requires(isa_required=ISA.X86)

# Arguments
parser = argparse.ArgumentParser(
    description="Configuration script to run the pointer array microbenchmark on a local DDR5 memory system"
)

# Full system mode vs SE mode
parser.add_argument(
    "-fs", "--full-system", action="store_true", help="Run in full system mode"
)

# Benchmark specific arguments
parser.add_argument(
    "--num-threads",
    type=str,
    default="64",
    required=True,
    # TODO: need better help message
    help="Specify the number of threads 1 - 1000",
)

parser.add_argument(
    "--array-size",
    type=str,
    required=True,
    # TODO: need better help message
    help="Specify the array size 1 - 1_000_000",
)

parser.add_argument(
    "--num-operations",
    type=str,
    required=True,
    # TODO: need better help message
    help="Specify the number of operations per thread 1 - 1_000_000",
)
parser.add_argument(
    "--random-distr",
    type=str,
    required=True,
    help="Specify random distribution ('u' for uniform or 'z' for  zipf)",
    choices=["u", "z"],
)

args = parser.parse_args()

FS_MODE = args.full_system
num_threads = args.num_threads
array_size = args.array_size
num_operations = args.num_operations
random_distr = args.random_distr

# Arguments for the binary (pointer array worload)
arguments = [num_threads, array_size, num_operations, random_distr]

# A single local DDR5 memory system.
#
# 3GiB in FS mode, not 4: x86 reserves [3GiB, 4GiB) for the I/O hole, so
# X86Board splits anything larger into two ranges, and a ChanneledMemory only
# accepts one contiguous range. SE mode has no such hole and keeps the full
# 4GiB. The benchmark's working set is a few KB either way.
memory = DIMM_DDR5_8400(size="3GiB" if FS_MODE else "4GiB")


# In this setup we don't have a cache. `NoCache` can be used for such setups.
# TODO: Make cache hierarchy an argument?
cache_hierarchy = NoCache()

# Set the workload
# Actual binary for SE, binary to copy over for FS
hostname = gethostname()
if hostname.startswith("Vadym"):  # 'Vadyms-MacBook-Air-2.local'
    WORKLOAD_PATH = "/Users/vadymmusiienko/Work/Research/microtests/bin-intel/microtest"
elif hostname == "pcal03":
    WORKLOAD_PATH = "/home/vmmv2023/SURP/microtests/bin/microtest"
elif "SLURM_JOB_ID" in os.environ or hostname == "sagehen.hpc.pomona.edu":
    WORKLOAD_PATH = "/rhome/vmmv2023/SURP/microtests/bin/microtest"
else:
    raise Exception("Not one of the configured machines! (pcal|hpc|local mac)")

# Kernel and disk image for FS mode, read straight off the local filesystem.
#
# `obtain_resource("...")` is deliberately not used: it queries
# https://api.gem5.org for the resource metadata *before* it ever looks in the
# cache, so it needs outbound internet on every run even when the files are
# already downloaded. HPC compute nodes have none, so every Slurm task died in
# that call before the board was built. These are the exact paths
# `obtain_resource` would have cached to, so an existing cache just works.
#
# Populate this directory once from a machine with internet: download the
# kernel and disk image (URLs + md5s in the FS-mode design doc under
# microtests/docs/), decompress the image, and name both files exactly as
# below. Or just copy them from another machine's ~/.cache/gem5.
RESOURCE_DIR = Path(
    os.environ.get("GEM5_RESOURCE_DIR", Path.home() / ".cache" / "gem5")
)
KERNEL_PATH = RESOURCE_DIR / "x86-linux-kernel-5.4.49-1.0.0"
DISK_IMAGE_PATH = RESOURCE_DIR / "x86-ubuntu-18.04-img-1.0.0"

# Full system mode setup
if FS_MODE:
    # Boot on ATOMIC cores (fast, and needs no host support), then switch to
    # TIMING for the region of interest so the benchmark sees real memory timing.
    processor = SimpleSwitchableProcessor(
        starting_core_type=CPUTypes.ATOMIC,
        switch_core_type=CPUTypes.TIMING,
        isa=ISA.X86,
        num_cores=(int(num_threads) + 1),
    )

    # X86 board to run FS mode
    board = X86Board(
        clk_freq="3GHz",
        processor=processor,
        memory=memory,
        cache_hierarchy=cache_hierarchy,
    )

    # Variable with the entire benchmark binary
    raw_binary = None
    with open(WORKLOAD_PATH, "rb") as file:
        raw_binary = file.read()
    if not raw_binary:
        print("Couldn't read the binary file into a variable")
        exit()

    # Encoding for shell script
    payload = base64.b64encode(raw_binary).decode("ascii")

    # Commands to run after boot.
    # Copy over the binary (dynamically updates it) and run it, bracketed by two
    # `m5 exit` calls that mark the region of interest. The binary is not m5
    # annotated, so plain exits are the ROI markers (see roi_exit_generator).
    # The base64 decode stays on the ATOMIC side of the first marker to keep it fast.
    my_commands = (
        "echo 'Boot complete, updating the binary!'\n"
        "cat << 'B64EOF' | base64 -d > /root/executable\n"
        f"{payload}\n"
        "B64EOF\n"
        "chmod +x /root/executable\n"
        "m5 exit\n"  # ROI start: switch to TIMING and reset stats
        f"/root/executable {' '.join(arguments)}\n"
        "m5 exit\n"  # ROI end: dump stats and end the simulation
    )

    # gem5 would report the missing file itself, but a Slurm log is the only
    # diagnostic these runs leave behind, so say what to do about it.
    for path in (KERNEL_PATH, DISK_IMAGE_PATH):
        if not path.is_file():
            raise FileNotFoundError(
                f"Full-system resource not found: {path}\n"
                "Copy the (decompressed) kernel and disk image into this "
                "directory, or point GEM5_RESOURCE_DIR at one that holds "
                "them. Download URLs are in the FS-mode design doc."
            )

    board.set_kernel_disk_workload(
        kernel=KernelResource(local_path=str(KERNEL_PATH)),
        disk_image=DiskImageResource(
            local_path=str(DISK_IMAGE_PATH), root_partition="1"
        ),
        readfile_contents=my_commands,  # Bash script that runs after boot
    )

    print("Full-System mode")

    # The guest's own gem5_init.sh issues no `m5 exit` before running our script
    # (it only exits after it returns), so our two markers are exits #1 and #2.
    # We never reach its trailing exit because marker #2 ends the simulation.
    def roi_exit_generator():
        print("Done booting Linux")
        print("ROI start: switching to TIMING cores and resetting stats")
        processor.switch()
        m5.stats.reset()
        yield False

        print("ROI end: dumping stats")
        m5.stats.dump()
        yield True

    on_exit_event = {ExitEvent.EXIT: roi_exit_generator()}

else:
    # SE mode setup
    processor = SimpleProcessor(
        cpu_type=CPUTypes.TIMING, isa=ISA.X86, num_cores=(int(num_threads) + 1)
    )

    board = SimpleBoard(
        clk_freq="3GHz",
        processor=processor,
        memory=memory,
        cache_hierarchy=cache_hierarchy,
    )

    binary = BinaryResource(local_path=WORKLOAD_PATH)
    board.set_se_binary_workload(binary=binary, arguments=arguments)

    # SE mode runs on TIMING from the start, so it needs no ROI markers and
    # keeps gem5's default exit behavior.
    on_exit_event = None

    print("System-call Emulation mode")


# Lastly we run the simulation.
simulator = Simulator(board=board, on_exit_event=on_exit_event)

print("Running the simulation")

simulator.run()
