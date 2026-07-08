from m5.params import *
from m5.SimObject import SimObject


class CXLcontroller(SimObject):
    type = "CXLcontroller"
    cxx_header = "mem/cxl_controller.hh"
    cxx_class = "gem5::CXLcontroller"

    cpu_port = ResponsePort("CPU side port, receives requests")
    mem_port = RequestPort("Memory side port, sends requests")

    # TODO:
    # Memory address ranges per memory device
    device_ranges = VectorParam.AddrRange([], "Address range of each memory device")
    cxl_strategy = Param.String(
        "direct",
        "CXL Controller redirect strategy: 'direct' | 'random' | 'speed' ",
    )

    # Fragmentation params ('direct' strategy only)
    frag_perc = Param.Int(0, "Percentage of granules to shuffle (0 = no fragmentation)")
    frag_seed = Param.UInt64(47, "Seed for the fragmentation shuffle")
