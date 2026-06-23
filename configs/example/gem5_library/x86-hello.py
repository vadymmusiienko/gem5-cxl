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

# from gem5.components.boards.simple_board import SimpleBoard
from gem5.components.boards.x86_board import X86Board
from gem5.components.cachehierarchies.classic.no_cache import NoCache
from gem5.components.memory.cxl_memory import CXLmemory

# --- Memory imports ---
from gem5.components.memory.dram_interfaces.ddr4 import DDR4_2400_8x8
from gem5.components.processors.cpu_types import CPUTypes
from gem5.components.processors.simple_processor import SimpleProcessor
from gem5.isas import ISA
from gem5.resources.resource import obtain_resource
from gem5.simulate.simulator import Simulator
from gem5.utils.requires import requires

# TODO: Add more memory (fast, medium, slow)
# --- Memory imports end ---


# This check ensures the gem5 binary contains the X86 ISA target. If not, an
# exception will be thrown.
requires(isa_required=ISA.X86)

# Arguments

# TODO: add desc
parser = argparse.ArgumentParser()
parser.add_argument(
    "--strategy",
    type=str,
    required=True,
    help="TODO",
    choices=["direct", "random", "speed"],
)
args = parser.parse_args()
# NOTE: There are going to be 3 strategies: "direct" | "random" | "speed"
strategy = args.strategy

# --- Instantiate memory ---

# TODO: Custom sizes? + different memories
fast_mem = DDR4_2400_8x8()
medium_mem = DDR4_2400_8x8()
slow_mem = DDR4_2400_8x8()

memory = [fast_mem, medium_mem, slow_mem]
# sizes = ["512MB", "512MB", "512MB"]
# sizes = ["16384MB", "16384MB", "16384MB"]
sizes = ["3GiB", "3GiB", "3GiB"]

cxl_mem = CXLmemory(memory=memory, sizes=sizes, strategy=strategy)
# --- Instantiate memory end ---


# In this setup we don't have a cache. `NoCache` can be used for such setups.
cache_hierarchy = NoCache()

# We use a simple Timing processor with one core.
processor = SimpleProcessor(cpu_type=CPUTypes.TIMING, isa=ISA.X86, num_cores=1)

# The gem5 library simple board which can be used to run SE-mode simulations.
board = X86Board(
    clk_freq="3GHz",
    processor=processor,
    memory=cxl_mem,
    cache_hierarchy=cache_hierarchy,
)

# Here we set the workload. In this case we want to run a simple "Hello World!"
# program compiled to the ARM ISA. The `obtain_resource` function will
# automatically download the binary from the gem5 Resources cloud bucket if
# it's not already present.
board.set_se_binary_workload(
    obtain_resource("x86-hello64-static", resource_version="1.0.0")
)

# Lastly we run the simulation.
simulator = Simulator(board=board)
simulator.run()
