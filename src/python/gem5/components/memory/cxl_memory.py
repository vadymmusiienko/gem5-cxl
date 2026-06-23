# Copyright (c) 2012, 2014, 2017-2019, 2021 Arm Limited
# All rights reserved
#
# The license below extends only to copyright in the software and shall
# not be construed as granting a license to any other intellectual
# property including but not limited to intellectual property relating
# to a hardware implementation of the functionality of the software
# licensed hereunder.  You may use the software subject to the license
# terms below provided that you ensure that this notice is replicated
# unmodified and in its entirety in all distributions of the software,
# modified or unmodified, in source code or in binary form.
#
# Copyright (c) 2002-2005 The Regents of The University of Michigan
# All rights reserved.
#
# Redistribution and use in source and binary forms, with or without
# modification, are permitted provided that the following conditions are
# met: redistributions of source code must retain the above copyright
# notice, this list of conditions and the following disclaimer;
# redistributions in binary form must reproduce the above copyright
# notice, this list of conditions and the following disclaimer in the
# documentation and/or other materials provided with the distribution;
# neither the name of the copyright holders nor the names of its
# contributors may be used to endorse or promote products derived from
# this software without specific prior written permission.
#
# THIS SOFTWARE IS PROVIDED BY THE COPYRIGHT HOLDERS AND CONTRIBUTORS
# "AS IS" AND ANY EXPRESS OR IMPLIED WARRANTIES, INCLUDING, BUT NOT
# LIMITED TO, THE IMPLIED WARRANTIES OF MERCHANTABILITY AND FITNESS FOR
# A PARTICULAR PURPOSE ARE DISCLAIMED. IN NO EVENT SHALL THE COPYRIGHT
# OWNER OR CONTRIBUTORS BE LIABLE FOR ANY DIRECT, INDIRECT, INCIDENTAL,
# SPECIAL, EXEMPLARY, OR CONSEQUENTIAL DAMAGES (INCLUDING, BUT NOT
# LIMITED TO, PROCUREMENT OF SUBSTITUTE GOODS OR SERVICES; LOSS OF USE,
# DATA, OR PROFITS; OR BUSINESS INTERRUPTION) HOWEVER CAUSED AND ON ANY
# THEORY OF LIABILITY, WHETHER IN CONTRACT, STRICT LIABILITY, OR TORT
# (INCLUDING NEGLIGENCE OR OTHERWISE) ARISING IN ANY WAY OUT OF THE USE
# OF THIS SOFTWARE, EVEN IF ADVISED OF THE POSSIBILITY OF SUCH DAMAGE.
#
# Author: Vadym Musiienko, Pomona College 2026
from typing import (
    List,
    Optional,
    Sequence,
    Tuple,
)

from m5.objects import AbstractMemory
from m5.objects import (
    AddrRange,
    BadAddr,
    BaseXBar,
    CXLcontroller,
    MemCtrl,
    NoncoherentXBar,
    Port,
)
from m5.util.convert import toMemorySize

from ...utils.override import overrides
from ..boards.abstract_board import AbstractBoard
from .abstract_memory_system import AbstractMemorySystem
from .dram_interfaces.ddr4 import (
    DDR4_2400_8x8,  # TODO: Do i even need a default?
)


class CXLmemory(AbstractMemorySystem):
    "A class that implements CXL Controller Memory Pool"

    # Memory side bus (connects Controller to Memory Pool)
    # Defaults to NoncoherentXBar with 64 width
    def _get_default_membus(self) -> NoncoherentXBar:
        # TODO: Change latency?
        membus = NoncoherentXBar(
            width=64,
            forward_latency=0,
            response_latency=0,
            frontend_latency=0,
            header_latency=0,
        )
        membus.badaddr_responder = BadAddr()
        membus.default = membus.badaddr_responder.pio
        return membus

    # TODO: Change to multiple memory devices
    # Memory in the CXL Memory Pool
    # Defaults to DDR4_2400_8x8 (1 device)
    def _get_default_memory(self, size) -> DDR4_2400_8x8:
        return [DDR4_2400_8x8(size)]

    def __init__(
        self,
        # latency: int
        strategy: Optional[str] = None,
        sizes: Optional[List[str]] = None,
        membus: Optional[BaseXBar] = None,
        memory: Optional[List[AbstractMemory]] = None,
    ) -> None:
        """
        :param strategy: One of the 3 strategies: "direct", "random", "speed". Defaults to "direct".
        :param sizes: The sizes of every memory device. Defaults to 1 size of "512MB"
        :param membus: The memory bus. This parameter is optional and will default to a 64 bit width SystemXBar if not specified.
        :param memory: A list of memory interfaces for the cxl memory pool.
        """
        super().__init__()

        # CXL Controller redirect strategy: "direct" | "random" | "speed"
        self._strategy = strategy if strategy else "direct"

        # Memory interfaces
        self.membus = membus if membus else self._get_default_membus()

        # Convert size strings to bytes
        self._sizes = (
            [toMemorySize(s) for s in sizes] if sizes else [toMemorySize("512MB")]
        )

        # CXL Memory pool devices
        self._dram = memory if memory else self._get_default_memory(sizes[0])
        assert len(memory) == len(sizes)  # Sanity check

        # Memory controller (Actually manages memory)
        self.mem_ctrls = [MemCtrl(dram=dram) for dram in self._dram]

        # My custom cxl controller
        self.cxl_ctrl = CXLcontroller()

        # Peer ports (CXL controller <-> Membus (Memory Pool side bus))
        self.cxl_ctrl.mem_port = self.membus.cpu_side_ports

        # Connect bus to all mem devices via ports (Membus <-> Memory Pool)
        for ctrl in self.mem_ctrls:
            ctrl.port = self.membus.mem_side_ports

    @overrides(AbstractMemorySystem)
    def incorporate_memory(self, board: AbstractBoard) -> None:
        # TODO: Should I connect peer ports here?

        # Pass our params to c++ backend
        self.cxl_ctrl.device_ranges = [dram.range for dram in self._dram]
        self.cxl_ctrl.cxl_strategy = self._strategy

    # Total size of the memory (total RAM)
    @overrides(AbstractMemorySystem)
    def get_size(self) -> int:
        return sum(self._sizes)

    # NOTE: The X86Board splits memory around the 3GiB-4GiB I/O hole
    # Made it work with X86Board of larger sizes than 3GiB
    @overrides(AbstractMemorySystem)
    def set_memory_range(self, ranges: List[AddrRange]) -> None:
        if len(ranges) < 1:
            raise Exception("CXL controller requires at least one memory range")

        # TODO:? The ranges handed to us must exactly match this memory's total size
        total_range_size = sum(int(r.size()) for r in ranges)
        if total_range_size != self.get_size():
            raise Exception(
                "CXL memory: the address ranges provided by the board "
                f"({total_range_size} B) do not match this memory's total "
                f"size ({self.get_size()} B)."
            )

        # NOTE: Must match BLOCK_SIZE in src/mem/cxl_controller.hh.
        BLOCK_SIZE = 64

        range_idx = 0
        cur = int(ranges[range_idx].start)
        range_end = int(ranges[range_idx].end)

        for i, size in enumerate(self._sizes):
            # Skip full ranges
            while cur >= range_end:
                range_idx += 1
                if range_idx >= len(ranges):
                    raise Exception("CXL memory: ran out of board address ranges")
                cur = int(ranges[range_idx].start)
                range_end = int(ranges[range_idx].end)

            # NOTE: Try to fit an entire mem device within the current range
            # TODO: Not sur if a mem device can be split between 2 ranges
            if cur + size > range_end:
                raise Exception(
                    "The first device/devices must be exactly 3GiB to fit in the first mem range of the X86Board."
                )

            # Ensure the addresses are block aligned
            assert cur % BLOCK_SIZE == 0 and size % BLOCK_SIZE == 0

            self._dram[i].range = AddrRange(start=cur, size=size)
            cur += size

    # Expose the controller's single CPU-side port to the board.
    @overrides(AbstractMemorySystem)
    def get_mem_ports(self) -> Sequence[Tuple[AddrRange, Port]]:
        start = min(int(dram.range.start) for dram in self._dram)
        end = max(int(dram.range.end) for dram in self._dram)
        size = end - start
        return [
            (
                AddrRange(start=start, size=size),
                self.cxl_ctrl.cpu_port,
            )
        ]

    # Expose memory controllers
    @overrides(AbstractMemorySystem)
    def get_memory_controllers(self) -> List[MemCtrl]:
        return self.mem_ctrls
