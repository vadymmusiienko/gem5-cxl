"""
This gem5 configuation script creates a simple board to run an X86
"hello world" binary.

This setup is close to the simplest setup possible using the gem5
library. It does not contain any kind of caching, IO, or any non-essential
components.

Usage
-----

```
scons build/ALL/gem5.opt
./build/ALL/gem5.opt configs/example/gem5_library/x86-hello.py
```
"""

import argparse

import m5

# from gem5.components.boards.simple_board import SimpleBoard
from gem5.components.boards.x86_board import X86Board
from gem5.components.cachehierarchies.classic.no_cache import NoCache
from gem5.components.memory.cxl_memory import CXLmemory

# --- Memory imports ---
from gem5.components.memory.dram_interfaces.ddr4 import DDR4_2400_8x8
from gem5.components.processors.cpu_types import CPUTypes

# from gem5.components.processors.simple_processor import SimpleProcessor
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

# TODO: Add more memory (fast, medium, slow)


# This check ensures the gem5 binary contains the X86 ISA target. If not, an
# exception will be thrown.
# TODO: require: , kvm_required = True
requires(isa_required=ISA.X86)

# Arguments
parser = argparse.ArgumentParser(
    description="Configuration script to run the GAPBS benchmarks on CXL memory system"
)

# CXL redirection strategy argument
parser.add_argument(
    "--strategy",
    type=str,
    required=True,
    help="Input the CXL controller redirect strategy to use.",
    choices=["direct", "random", "speed"],
)

# The benchmark name argument
parser.add_argument(
    "--benchmark",
    type=str,
    required=True,
    help="Input the benchmark program to execute.",
    choices=["bfs", "pr_spmv", "pr", "cc", "tc", "bc", "sssp", "cc_sv"],
)


args = parser.parse_args()

# --- Instantiate memory ---

# TODO: Custom sizes? + different memories
fast_mem = DDR4_2400_8x8()
medium_mem = DDR4_2400_8x8()
slow_mem = DDR4_2400_8x8()

memory = [fast_mem, medium_mem, slow_mem]
# sizes = ["1GiB", "1GiB", "1GiB"]
sizes = ["512MiB", "512MiB", "512MiB"]
# sizes = ["16384MB", "16384MB", "16384MB"]
# NOTE: There are going to be 3 strategies: "direct" | "random" | "speed"
strategy = args.strategy


cxl_mem = CXLmemory(memory=memory, sizes=sizes, strategy=strategy)
# --- Instantiate memory end ---


# In this setup we don't have a cache. `NoCache` can be used for such setups.
cache_hierarchy = NoCache()

# We use a simple Timing processor with one core.
# processor = SimpleProcessor(cpu_type=CPUTypes.TIMING, isa=ISA.X86, num_cores=1)

# TODO: processor that supports KVM
# TODO: can change KVM to ATOMIC
processor = SimpleSwitchableProcessor(
    starting_core_type=CPUTypes.ATOMIC,
    switch_core_type=CPUTypes.TIMING,
    isa=ISA.X86,
    num_cores=2,
)

# TODO: Do i need perf?
# Here we tell the KVM CPU (the starting CPU) not to use perf.
# for proc in processor.start:
#     proc.core.usePerf = False

# The gem5 library simple board which can be used to run SE-mode simulations.
# TODO: Changed board to run linux
board = X86Board(
    clk_freq="3GHz",
    processor=processor,
    memory=cxl_mem,
    cache_hierarchy=cache_hierarchy,
)

# board = SimpleBoard(
#     clk_freq="3GHz",
#     processor=processor,
#     memory=cxl_mem,
#     cache_hierarchy=cache_hierarchy,
# )

# Here we set the workload. In this case we want to run a simple "Hello World!"
# program compiled to the ARM ISA. The `obtain_resource` function will
# automatically download the binary from the gem5 Resources cloud bucket if
# it's not already present.

board.set_workload(
    obtain_resource(
        f"x86-ubuntu-24.04-gapbs-{args.benchmark}-test",
        resource_version="1.0.0",
    )
)

# board.set_se_binary_workload(
#     obtain_resource("x86-hello64-static", resource_version="1.0.0")
# )


# TODO: Do i need this?
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


# Lastly we run the simulation.
simulator = Simulator(board=board)

print("Running the simulation")
print("Using KVM cpu")

simulator.run()
