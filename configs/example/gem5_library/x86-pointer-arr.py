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
from socket import gethostname

import m5
from gem5.components.boards.simple_board import SimpleBoard
from gem5.components.boards.x86_board import X86Board
from gem5.components.cachehierarchies.classic.no_cache import NoCache

# --- Memory imports ---
from gem5.components.memory import DIMM_DDR5_8400
from gem5.components.processors.cpu_types import CPUTypes
from gem5.components.processors.simple_processor import SimpleProcessor
from gem5.components.processors.simple_switchable_processor import (  # TODO: processor that supports KVM
    SimpleSwitchableProcessor,
)
from gem5.isas import ISA

# from gem5.resources.resource import BinaryResource, obtain_resource
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

# This check ensures the gem5 binary contains the X86 ISA target. If not, an
# exception will be thrown.
# TODO: require: , kvm_required = True
requires(isa_required=ISA.X86)

# Arguments
parser = argparse.ArgumentParser(
    description="Configuration script to run the pointer array microbenchmark on a local DDR5 memory system"
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
USE_KVM = args.kvm
num_threads = args.num_threads
array_size = args.array_size
num_operations = args.num_operations
random_distr = args.random_distr

# Arguments for the binary (pointer array worload)
arguments = [num_threads, array_size, num_operations, random_distr]

# A single local DDR5 memory system
memory = DIMM_DDR5_8400(size="4GiB")


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
        memory=memory,
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
