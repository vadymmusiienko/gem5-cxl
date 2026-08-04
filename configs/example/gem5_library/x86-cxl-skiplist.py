"""
This gem5 configuation script creates a simple board to run an X86
"microtest" benchmark binary. (Array of pointers)

This setup is close to the simplest setup possible using the gem5
library. It does not contain any kind of caching, IO, or any non-essential
components.

Usage
-----

```
scons build/ALL/gem5.opt
./build/ALL/gem5.opt configs/example/gem5_library/x86-cxl-microbenchmark.py
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
from gem5.components.memory.cxl_memory import CXLmemory

# --- Memory imports ---
from gem5.components.memory.dram_interfaces.ddr3 import DDR3_1600_8x8
from gem5.components.memory.dram_interfaces.ddr4 import DDR4_2400_8x8
from gem5.components.memory.dram_interfaces.ddr5 import DDR5_8400_4x8
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

# TODO: Add more memory (fast, medium, slow)


# This check ensures the gem5 binary contains the X86 ISA target. If not, an
# exception will be thrown.
requires(isa_required=ISA.X86)

# Arguments
parser = argparse.ArgumentParser(
    description="Configuration script to run skiplist microbenchmark on CXL memory system"
)

# CXL redirection strategy argument
parser.add_argument(
    "--strategy",
    type=str,
    required=True,
    help="Input the CXL controller redirect strategy to use.",
    choices=["direct", "random", "speed"],
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
    "--initial-size",
    type=str,
    required=True,
    default="256",
    # TODO: need better help message
    help="Specify the inital skiplist size 1 - 1_000_000",
)

parser.add_argument(
    "--range",
    type=str,
    required=True,
    default="2048",
    # TODO: need better help message
    help="Specify the range of set keys (keyspace)",
)
parser.add_argument(
    "--duration",
    type=str,
    required=True,
    default="10000",
    help="Specify duration",
)

# CXL controller round-trip latency
parser.add_argument(
    "--latency",
    type=int,
    default=25,
    help="Specify cxl controller round-trip latency in nanoseconds",
)

# Direct method fragmentation arguments
# NOTE: Granularity is a constant (FRAG_GRANULE in src/mem/cxl_controller.hh)
parser.add_argument(
    "--frag-perc",
    type=int,
    default=0,
    help="Percentage of memory granules to shuffle (direct strategy only)",
)

parser.add_argument(
    "--frag-seed",
    type=int,
    default=47,
    help="Seed for the fragmentation shuffle (direct strategy only)",
)

args = parser.parse_args()

# NOTE: There are going to be 3 strategies: "direct" | "random" | "speed"
strategy = args.strategy
FS_MODE = args.full_system
num_threads = args.num_threads
initial_size = args.initial_size
keyspace_range = args.range
duration = args.duration

# Fragmentation only makes sense for the direct strategy
if strategy != "direct" and args.frag_perc != 0:
    parser.error("--frag-perc only applies to the 'direct' strategy")

# TODO: Take as arguments?
sizes = ["1GiB", "1GiB", "1GiB"]
# sizes = ["512MiB", "512MiB", "512MiB"]
# sizes = ["3GiB", "16384MB", "16384MB"]

# Arguments for the binary (skiplist)
arguments = [
    "-d",
    duration,
    "-t",
    num_threads,
    # "-u", update_perc,
    "-i",
    initial_size,
    "-r",
    keyspace_range,
]

# TODO: Custom sizes? + different memories
# DDR5, DDR4, DDR3
fast_mem = DDR5_8400_4x8()
medium_mem = DDR4_2400_8x8()
slow_mem = DDR3_1600_8x8()

# NOTE: First memory device(s) have to be exactly 3GiB (or total memory <= 3GiB)
memory = [fast_mem, medium_mem, slow_mem]

cxl_mem = CXLmemory(
    memory=memory,
    sizes=sizes,
    strategy=strategy,
    frag_perc=args.frag_perc,
    frag_seed=args.frag_seed,
    cxl_latency=args.latency,
)


# In this setup we don't have a cache. `NoCache` can be used for such setups.
# TODO: Make cache hierarchy an argument?
cache_hierarchy = NoCache()

# Set the workload
# Actual binary for SE, binary to copy over for FS
hostname = gethostname()
if hostname.startswith("Vadym"):  # 'Vadyms-MacBook-Air-2.local'
    WORKLOAD_PATH = "/Users/vadymmusiienko/Work/Research/microtests/bin-intel/lockfree-fraser-skiplist"
elif hostname == "pcal03":
    WORKLOAD_PATH = "/home/vmmv2023/SURP/microtests/bin/lockfree-fraser-skiplist"
elif "SLURM_JOB_ID" in os.environ or hostname == "sagehen.hpc.pomona.edu":
    WORKLOAD_PATH = "/rhome/vmmv2023/SURP/microtests/bin/lockfree-fraser-skiplist"
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
        memory=cxl_mem,
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
        memory=cxl_mem,
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
