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
from gem5.components.processors.simple_switchable_processor import (  # TODO: processor that supports KVM
    SimpleSwitchableProcessor,
)
from gem5.isas import ISA
from gem5.resources.resource import (
    BinaryResource,
    obtain_resource,
)
from gem5.simulate.exit_handler import (
    WorkBeginExitHandler,
    WorkEndExitHandler,
)
from gem5.simulate.simulator import Simulator
from gem5.utils.override import overrides
from gem5.utils.requires import requires

# TODO: Add more memory (fast, medium, slow)


# This check ensures the gem5 binary contains the X86 ISA target. If not, an
# exception will be thrown.
# TODO: require: , kvm_required = True
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

# Use KVM for the boot phase (FS mode only)
parser.add_argument(
    "-kvm",
    "--kvm",
    action="store_true",
    help="Use KVM instead of ATOMIC for the boot phase (FS mode only). "
    "Requires an x86 host with /dev/kvm. Defaults to ATOMIC.",
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
USE_KVM = args.kvm
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

# Full system mode setup
if FS_MODE:
    # KVM is faster than Atomic
    if USE_KVM:
        requires(kvm_required=True)
    starting_core_type = CPUTypes.KVM if USE_KVM else CPUTypes.ATOMIC

    # Switchable Processor to run FS mode
    processor = SimpleSwitchableProcessor(
        starting_core_type=starting_core_type,
        switch_core_type=CPUTypes.TIMING,
        isa=ISA.X86,
        num_cores=(int(num_threads) + 1),
    )

    # Disable perf on all KVM cores
    for core in processor.get_cores():
        if core.is_kvm_core():
            core.get_simobject().usePerf = False

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

    # Commands to run after boot
    # Copy over the binary (dynamically updates it) and run it
    # TODO: try without wrap
    my_commands = (
        "echo 'Boot complete, updating the binary!'\n"
        "cat << 'B64EOF' | base64 -d > /root/executable\n"
        f"{payload}\n"
        "B64EOF\n"
        "chmod +x /root/executable\n"
        f"/root/executable {' '.join(arguments)}\n"
        "m5 exit\n"
    )

    board.set_kernel_disk_workload(
        kernel=obtain_resource(resource_id="x86-linux-kernel-5.4.49"),
        disk_image=obtain_resource(resource_id="x86-ubuntu-18.04-img"),
        readfile_contents=my_commands,  # Bash script that runs after boot
    )

    print("Full-System mode")

    # Custom messages
    class CustomWorkBeginExitHandler(WorkBeginExitHandler):
        @overrides(WorkBeginExitHandler)
        def _process(self, simulator: "Simulator") -> None:
            print("Done booting Linux")
            print("Resetting stats at the start of ROI!")
            m5.stats.reset()
            simulator.switch_processor()

        @overrides(WorkBeginExitHandler)
        def _exit_simulation(self) -> bool:
            return False

    class CustomWorkEndExitHandler(WorkEndExitHandler):
        @overrides(WorkEndExitHandler)
        def _process(self, simulator: "Simulator") -> None:
            print("Dump stats at the end of the ROI!")
            m5.stats.dump()

        @overrides(WorkEndExitHandler)
        def _exit_simulation(self) -> bool:
            return True

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

    print("System-call Emulation mode")


# Lastly we run the simulation.
simulator = Simulator(board=board)

print("Running the simulation")
# print("Using KVM cpu")

simulator.run()
