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
from socket import gethostname
import os

import m5

from m5.util.convert import toMemorySize  # Convert mem sizes to bytes (int)
from gem5.components.boards.x86_board import X86Board
from gem5.components.boards.simple_board import SimpleBoard
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
from gem5.resources.resource import obtain_resource
from gem5.simulate.exit_handler import (
    WorkBeginExitHandler,
    WorkEndExitHandler,
)
from gem5.simulate.simulator import Simulator
from gem5.utils.override import overrides
from gem5.utils.requires import requires
from gem5.resources.resource import BinaryResource

# TODO: Add more memory (fast, medium, slow)


# This check ensures the gem5 binary contains the X86 ISA target. If not, an
# exception will be thrown.
# TODO: require: , kvm_required = True
requires(isa_required=ISA.X86)

# Arguments
parser = argparse.ArgumentParser(
    description="Configuration script to run the pointer array microbenchmark on CXL memory system"
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

# NOTE: There are going to be 3 strategies: "direct" | "random" | "speed"
strategy = args.strategy
FS_MODE = args.full_system
num_threads = args.num_threads
array_size = args.array_size
num_operations = args.num_operations
random_distr = args.random_distr

# TODO: Take as arguments?
sizes = ["1GiB", "1GiB", "1GiB"]
# sizes = ["512MiB", "512MiB", "512MiB"]
# sizes = ["3GiB", "16384MB", "16384MB"]

# Direct method fragmentation params
# TODO: Take as arguments?
total_size = None
free_mem_perc = None
if strategy == "direct":
    total_size = sum(toMemorySize(size) for size in sizes)
    free_mem_perc = 80

# Arguments for the binary (pointer array worload)
arguments = [num_threads, array_size, num_operations, random_distr]
if (total_size is not None) and (free_mem_perc is not None):
    arguments.extend([str(total_size), str(free_mem_perc), "--mem-safe"])

# TODO: Custom sizes? + different memories
# DDR5, DDR4, DDR3
fast_mem = DDR5_8400_4x8()
medium_mem = DDR4_2400_8x8()
slow_mem = DDR3_1600_8x8()

# NOTE: First memory device(s) have to be exactly 3GiB (or total memory <= 3GiB)
memory = [fast_mem, medium_mem, slow_mem]

cxl_mem = CXLmemory(memory=memory, sizes=sizes, strategy=strategy)


# In this setup we don't have a cache. `NoCache` can be used for such setups.
# TODO: Make cache hierarchy an argument?
cache_hierarchy = NoCache()

# Full system mode setup
if FS_MODE:
    # TODO: processor that supports KVM
    # TODO: can change KVM to ATOMIC
    # Switchable Processor to run FS mode3
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

    # # FS mode workdload setting
    # # TODO: Change workload to my disk image
    # board.set_workload(
    #     obtain_resource(
    #         f"x86-ubuntu-24.04-gapbs-{args.benchmark}-test",
    #         resource_version="1.0.0",
    #     )
    # )
    #
    # class CustomWorkBeginExitHandler(WorkBeginExitHandler):
    #     @overrides(WorkBeginExitHandler)
    #     def _process(self, simulator: "Simulator") -> None:
    #         print("Done booting Linux")
    #         print("Resetting stats at the start of ROI!")
    #         m5.stats.reset()
    #         simulator.switch_processor()
    #
    #     @overrides(WorkBeginExitHandler)
    #     def _exit_simulation(self) -> bool:
    #         return False
    #
    # class CustomWorkEndExitHandler(WorkEndExitHandler):
    #     @overrides(WorkEndExitHandler)
    #     def _process(self, simulator: "Simulator") -> None:
    #         print("Dump stats at the end of the ROI!")
    #         m5.stats.dump()
    #
    #     @overrides(WorkEndExitHandler)
    #     def _exit_simulation(self) -> bool:
    #         return True

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

    # Set the workload
    # TODO: Don't hardcode the workload??
    hostname = gethostname()
    if hostname.startswith("Vadym"):  # 'Vadyms-MacBook-Air-2.local'
        WORKLOAD_PATH = (
            "/Users/vadymmusiienko/Work/Research/microtests/bin-intel/microtest"
        )
    elif hostname == "pcal03":
        WORKLOAD_PATH = "/home/vmmv2023/SURP/microtests/bin/microtest"
    elif "SLURM_JOB_ID" in os.environ or hostname == "sagehen.hpc.pomona.edu":
        WORKLOAD_PATH = "/rhome/vmmv2023/SURP/microtests/bin/microtest"
    else:
        raise Exception("Not one of the configured machines! (pcal|hpc|local mac)")

    binary = BinaryResource(local_path=WORKLOAD_PATH)
    board.set_se_binary_workload(binary=binary, arguments=arguments)


# Lastly we run the simulation.
simulator = Simulator(board=board)

print("Running the simulation")
# print("Using KVM cpu")

simulator.run()
