"""
Full-system config for the concurrent skip list on CXL device memory.

Mode is "operations" by default

The guest prints one CSV row on stdout. Run once with --mode header to get
the column names.

```
./build/X86/gem5.opt --outdir=m5out_skiplist_8t_random \
    configs/example/gem5_library/x86-cxl-skiplist-test.py \
    --threads 8 --preload 32000 --ops 20000 \
    --ins 25 --rem 25 --con 50 --element-space 1000000 --policy random
```
"""

import argparse
import base64
import os
from socket import gethostname

import m5
from gem5.components.boards.x86_board import X86Board
from gem5.components.cachehierarchies.classic.no_cache import NoCache
from gem5.components.memory.cxl_memory import CXLmemory
from gem5.components.memory.dram_interfaces.ddr3 import DDR3_1600_8x8
from gem5.components.memory.dram_interfaces.ddr4 import DDR4_2400_8x8
from gem5.components.memory.dram_interfaces.ddr5 import DDR5_8400_4x8
from gem5.components.processors.cpu_types import CPUTypes
from gem5.components.processors.simple_switchable_processor import (
    SimpleSwitchableProcessor,
)
from gem5.isas import ISA
from gem5.resources.resource import DiskImageResource, KernelResource
from gem5.simulate.exit_event import ExitEvent
from gem5.simulate.simulator import Simulator
from gem5.utils.requires import requires

requires(isa_required=ISA.X86)

parser = argparse.ArgumentParser(description="Skip list on CXL device memory")
parser.add_argument("--threads", type=int, default=4)

# NOTE: Use operations mode
parser.add_argument(
    "--mode",
    default="operations",
    choices=["operations", "header"],
)
parser.add_argument("--ins", type=int, default=25, help="insert percent")
parser.add_argument("--rem", type=int, default=25, help="remove percent")
parser.add_argument("--con", type=int, default=50, help="contains percent")
parser.add_argument("--preload", type=int, default=32000)
parser.add_argument("--element-space", type=int, default=1000000)
parser.add_argument("--ops", type=int, default=20000, help="operations per thread")
parser.add_argument("--seconds", type=int, default=10, help="only used by time modes")

# Fixed so two policies draw the same keys and the comparison is apples to
# apples. Vary it to get a different stream.
parser.add_argument("--seed", type=int, default=12345, help="guest RNG seed")

# Which device the allocator puts each object on. See DevicePolicy.hh.
parser.add_argument(
    "--policy",
    default="speed",
    choices=["random", "mirakuru", "speed"],
)

# DEVICE_SIZES in tests/SkipListTest.cc must match this list.
sizes = ["160MiB", "32MiB", "64MiB"]
memory = [DDR5_8400_4x8(), DDR4_2400_8x8(), DDR3_1600_8x8()]

cxl_mem = CXLmemory(
    memory=memory,
    sizes=sizes,
    strategy="speed",
    frag_perc=0,
    frag_seed=47,
    cxl_latency=25,
)

# For some reason we need extra cores (more cores than threads)
parser.add_argument(
    "--extra-cores",
    type=int,
    default=6,
    help="cores beyond --threads, for the main thread and the kernel",
)
args = parser.parse_args()

processor = SimpleSwitchableProcessor(
    starting_core_type=CPUTypes.ATOMIC,
    switch_core_type=CPUTypes.TIMING,
    isa=ISA.X86,
    num_cores=args.threads + args.extra_cores,
)

board = X86Board(
    clk_freq="3GHz",
    processor=processor,
    memory=cxl_mem,
    cache_hierarchy=NoCache(),
)

hostname = gethostname()
if hostname.startswith("Vadym"):
    BIN_DIR = "/Users/vadymmusiienko/Work/Research/microtests/bin"
elif hostname == "pcal03":
    BIN_DIR = "/home/vmmv2023/SURP/microtests/bin"
elif "SLURM_JOB_ID" in os.environ or hostname == "sagehen.hpc.pomona.edu":
    BIN_DIR = "/rhome/vmmv2023/SURP/microtests/bin"
else:
    print("Not one of the configured machines! (pcal|hpc|local mac)")
    assert False

WORKLOAD_PATH = os.path.join(BIN_DIR, "SkipListTest")

# Same cache layout obtain_resource() would have used, so an already
# populated ~/.cache/gem5 just works. Explicit paths because the cluster has
# no internet to download from.
RESOURCE_DIR = os.environ.get("GEM5_RESOURCE_DIR", os.path.expanduser("~/.cache/gem5"))
KERNEL_PATH = os.path.join(RESOURCE_DIR, "x86-linux-kernel-5.4.49-1.0.0")
DISK_IMAGE_PATH = os.path.join(RESOURCE_DIR, "x86-ubuntu-18.04-img-1.0.0")

for path in (KERNEL_PATH, DISK_IMAGE_PATH):
    if not os.path.isfile(path):
        print(f"Full-system resource not found: {path}")
        assert False

with open(WORKLOAD_PATH, "rb") as f:
    payload = base64.b64encode(f.read()).decode("ascii")

# NOTE: SkipList Tester takes 9 positional args in this order
# NOTE: policy is an additional argument for cxl skiplist only
GUEST_ARGS = (
    f"{args.threads} {args.mode} {args.ins} {args.rem} {args.con} "
    f"{args.preload} {args.element_space} {args.ops} {args.seconds} "
    f"--{args.policy} --seed {args.seed}"
)

my_commands = (
    "echo 'Boot complete, updating the binary!'\n"
    "cat << 'B64EOF' | base64 -d > /root/executable\n"
    f"{payload}\n"
    "B64EOF\n"
    "chmod +x /root/executable\n"
    "m5 exit\n"  # ROI start: switch to TIMING and reset stats
    f"/root/executable {GUEST_ARGS}\n"
    'echo "skiplist exit $?"\n'
    "m5 exit\n"  # ROI end: dump stats and end the simulation
)

board.set_kernel_disk_workload(
    kernel=KernelResource(local_path=KERNEL_PATH),
    disk_image=DiskImageResource(local_path=DISK_IMAGE_PATH, root_partition="1"),
    readfile_contents=my_commands,
)

print("Full-System mode")


# Two markers, two functions, in the order the boot script fires them. Each
# returns True to end the simulation loop, False to carry on. This is the same
# shape as x86-cxl-microbenchmark.py, just without the generator.
def roi_start() -> bool:
    print("ROI start: switching to TIMING cores and resetting stats")
    processor.switch()
    m5.stats.reset()
    return False


def roi_end() -> bool:
    print("ROI end: dumping stats")
    m5.stats.dump()
    return True


simulator = Simulator(
    board=board,
    on_exit_event={ExitEvent.EXIT: [roi_start, roi_end]},
)

print(
    f"skip list: {args.threads} threads, {args.mode}, {args.policy}, "
    f"seed {args.seed}, sizes {sizes}"
)
simulator.run()
