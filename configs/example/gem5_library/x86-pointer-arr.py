"""
This gem5 configuation script creates a simple board to run an X86
"pointer array microbenchmark" binary.

This setup is close to the simplest setup possible using the gem5
library. It does not contain any kind of caching, IO, or any non-essential
components.

Usage
-----

```
scons build/ALL/gem5.opt
./build/ALL/gem5.opt configs/example/gem5_library/x86-pointer-arr.py
```
"""

import argparse
from socket import (
    gethostname,
)  # TODO: Just for the binary path (pcal vs local mac vs hpc)
import os

from gem5.components.boards.simple_board import SimpleBoard

from gem5.components.cachehierarchies.classic.no_cache import NoCache
from gem5.components.memory import SingleChannelDDR3_1600
from gem5.components.processors.cpu_types import CPUTypes
from gem5.components.processors.simple_processor import SimpleProcessor
from gem5.isas import ISA
from gem5.simulate.simulator import Simulator

from gem5.components.cachehierarchies.classic.private_l1_cache_hierarchy import (
    PrivateL1CacheHierarchy,
)

from gem5.utils.requires import requires

# Imports for custom workload
# from gem5.components.boards.se_binary_workload import SEBinaryWorkload
from gem5.resources.resource import BinaryResource

# This check ensures the gem5 binary contains the X86 ISA target. If not, an
# exception will be thrown.
requires(isa_required=ISA.X86)

# --------- Arguments ---------

# TODO: add desc
parser = argparse.ArgumentParser()

parser.add_argument(
    "--num-threads",
    type=str,
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

num_threads = args.num_threads
array_size = args.array_size
num_operations = args.num_operations
random_distr = args.random_distr
NUM_CORES = 65

# TODO: Add cache similar to local machine
# TODO: Cache as an argument?
cache_hierarchy = NoCache()
# cache_hierarchy = PrivateL1CacheHierarchy(l1d_size="4KiB", l1i_size="32KiB")

# TODO: Add memory similar to local machine
# We use a single channel DDR3_1600 memory system
memory = SingleChannelDDR3_1600(size="4GiB")

# We use a simple Timing processor with one core.
# TODO: Choose a cpu similar to local machine
# TODO: Number of cores has to be >= number of threads (because no OS)
# TODO: Try either TIMING or O3
processor = SimpleProcessor(cpu_type=CPUTypes.TIMING, isa=ISA.X86, num_cores=NUM_CORES)

# TODO: Choose a board similar to local machine
# The gem5 library simple board which can be used to run SE-mode simulations.
board = SimpleBoard(
    clk_freq="3GHz",
    processor=processor,
    memory=memory,
    cache_hierarchy=cache_hierarchy,
)

# TODO: Don't hardcode the workload??
hostname = gethostname()
if hostname.startswith("Vadym"):  # 'Vadyms-MacBook-Air-2.local'
    WORKLOAD_PATH = "/Users/vadymmusiienko/Work/Research/microtests/bin-intel/microtest"
elif hostname == "pcal03":
    WORKLOAD_PATH = "/home/vmmv2023/SURP/microtests/bin/microtest"
elif "SLURM_JOB_ID" in os.environ or hostname == "sagehen.hpc.pomona.edu":
    WORKLOAD_PATH = "/rhome/vmmv2023/SURP/microtests/bin/microtest"
else:
    raise Exception("Not one of the configured machines! (pcal|hpc|local mac)")

# Set the workload
binary = BinaryResource(local_path=WORKLOAD_PATH)
arguments = [num_threads, array_size, num_operations, random_distr]

# board.set_se_binary_wordload(workload)
board.set_se_binary_workload(binary=binary, arguments=arguments)

# Run the simulation
simulator = Simulator(board=board)
simulator.run()
