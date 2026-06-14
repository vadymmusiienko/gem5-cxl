#include "mem/cxl_controller.hh"

#include <cstdint>

#include "base/addr_range.hh"
#include "base/logging.hh"
#include "mem/packet.hh"

namespace gem5
{

CXLcontroller::CXLcontroller(const CXLcontrollerParams *params)
    : SimObject(*params),
      cpu_port(params->name + ".cpu_port", this),
      mem_port(params->name + ".mem_port", this),
      device_addr_ranges(params->device_ranges),   // Config param
      cxl_redirect_strategy(params->cxl_strategy), // Config param
      addr_map(),                                  // Phys addr -> Device addr
      reverse_addr_map(),                          // Orig pkt -> New pkt
      device_next_block(), // Next free block of every device
      speed_device_idx(0)  // The index of the fastest device
{
    // Initialize next block of each device to the start of its range
    for (int i = 0; i < device_addr_ranges.size(); i++) {

        assert(device_addr_ranges[i].start() % BLOCK_SIZE == 0);
        assert(device_addr_ranges[i].size() % BLOCK_SIZE == 0);

        Addr start = device_addr_ranges[i].start();
        device_next_block.push_back(start);
    }
}

Port &
CXLcontroller::getPort(const std::string &if_name, PortID idx)
{
    panic_if(idx != InvalidPortID, "This object doesn't support vector ports");
    // These names come from CXLcontroller.py (mem_port, cpu_port)
    if (if_name == "mem_port") {
        return mem_port;
    } else if (if_name == "cpu_port") {
        return cpu_port;
    } else {
        // pass it along to our super class
        return SimObject::getPort(if_name, idx);
    }
}

// Copy a packet and update address
// Also handles alignment
// !! DOES NOT UPDATE device_next_block !!
PacketPtr
CXLcontroller::remapPacket(PacketPtr pkt, Addr new_addr_block, bool isTiming)
{
    // Alignment
    // Addr new_addr_block (param)            // Aligned device
    Addr origAddr = pkt->getAddr();           // Non-aligned phys
    Addr offset = origAddr % BLOCK_SIZE;      // == 0 if already aligned
    Addr newAddr = new_addr_block + offset;   // Non-aligned device
    Addr origAddrAligned = origAddr - offset; // Aligned phys

    // Phys addr -> Device addr (Block aligned)
    addr_map[origAddrAligned] = new_addr_block;

    // Don't create a new packet for functional and atomic requests
    if (!isTiming) {
        pkt->setAddr(newAddr);
        return pkt;
    }

    // Copy the old packet (keep the flags and create new data buffer)
    PacketPtr newPkt = new Packet(pkt, false, true);
    newPkt->setAddr(newAddr); // Overwrite the address

    // TODO: THIS WAS ANOTHER BUG (I DIDN'T HAVE THIS)
    // Copy over the data
    if (pkt->isWrite()) {
        newPkt->setData(pkt->getConstPtr<uint8_t>());
    }

    // Only add to reverse map if needs response
    if (pkt->needsResponse() && isTiming) {
        reverse_addr_map[newPkt] = pkt; // Device pkt -> Original pkt
    }

    return newPkt;
}

// Takes in an original packet and updates it for the response (copies the data
// over and makes it a response)
void
CXLcontroller::updateOldPacket(PacketPtr oldPkt, PacketPtr newPkt)
{
    // Make response
    if (oldPkt->needsResponse()) {
        oldPkt->makeResponse();
    }

    // TODO: THIS WAS THE BUG
    // CANNOT USE hasRespData() HERE BECAUSE IT IS FOR REQUESTS! (gem5 bug?)
    // Copy over the data
    if (newPkt->isResponse() && newPkt->hasData()) {
        oldPkt->setData(newPkt->getConstPtr<uint8_t>());
    }
}

// Takes in a pkt from cpu or memory and returns pkt (unchanged)
// Doesn't do anything
PacketPtr
CXLcontroller::handleDirect(PacketPtr pkt)
{ return pkt; }

// Takes in a pkt from cpu or memory and returns an appropriate packet to be
// sent further choosing a random device
PacketPtr
CXLcontroller::handleRandom(PacketPtr pkt, bool from_cpu, bool isTiming)
{
    if (from_cpu) {
        // Received a request packet from cpu side
        assert(pkt->getSize() <= BLOCK_SIZE);

        // Alignment
        Addr cpu_addr = pkt->getAddr();            // Non-aligned phys
        Addr offset = cpu_addr % BLOCK_SIZE;       // == 0 if already aligned
        Addr cpu_addr_aligned = cpu_addr - offset; // Aligned phys

        if (addr_map.find(cpu_addr_aligned) == addr_map.end()) {
            // First time seeing this address

            // Choose random device
            // TODO: Panic if out of memory everywhere
            // TODO: Optimize by removing full memory devices from possible
            // devices?
            int device_idx;
            Addr next_block;
            AddrRange range;
            do {
                device_idx = rand() % device_addr_ranges.size();
                range = device_addr_ranges[device_idx];
                next_block = device_next_block[device_idx];
            } while (range.end() == next_block);

            // Remap the packet
            PacketPtr newPkt = remapPacket(pkt, next_block, isTiming);

            // Update next free block for this device
            device_next_block[device_idx] += BLOCK_SIZE;

            return newPkt;

        } else {
            // Already mapped (exists)
            return remapPacket(pkt, addr_map[cpu_addr_aligned], isTiming);
        }
    } else {
        // Received a response packet from memory side

        // Find the original packet
        auto got = reverse_addr_map.find(pkt);
        panic_if(got == reverse_addr_map.end(),
                 "No original packet for addr 0x%x", pkt->getAddr());

        PacketPtr origPkt = got->second;

        // Copy over the response data
        updateOldPacket(origPkt, pkt);

        // Clean up
        reverse_addr_map.erase(got);
        delete pkt;

        return origPkt;
    }
}

// Takes in a pkt from cpu or memory and returns an appropriate packet to be
// sent further choosing the fastest device
// !! (Assumes that fastest device is first in device_addr_ranges) !!
PacketPtr
CXLcontroller::handleSpeed(PacketPtr pkt, bool from_cpu, bool isTiming)
{
    if (from_cpu) {
        // Received a request packet from cpu side
        assert(pkt->getSize() <= BLOCK_SIZE);

        // Alignment
        Addr cpu_addr = pkt->getAddr();            // Non-aligned phys
        Addr offset = cpu_addr % BLOCK_SIZE;       // == 0 if already aligned
        Addr cpu_addr_aligned = cpu_addr - offset; // Aligned phys

        if (addr_map.find(cpu_addr_aligned) == addr_map.end()) {
            // First time seeing this address

            // Choose fastest device
            assert(speed_device_idx < device_addr_ranges.size());
            AddrRange range = device_addr_ranges[speed_device_idx];
            Addr next_block = device_next_block[speed_device_idx];
            assert(range.end() != next_block);

            // Remap the packet
            PacketPtr newPkt = remapPacket(pkt, next_block, isTiming);

            // Update next free block for this device
            device_next_block[speed_device_idx] += BLOCK_SIZE;

            // Update speed memory index
            if (device_next_block[speed_device_idx] == range.end()) {
                speed_device_idx += 1;
                panic_if(speed_device_idx < device_next_block.size(),
                         "Ran out of memory across all CXL memory devices");
            }

            return newPkt;

        } else {
            // Already mapped (exists)
            return remapPacket(pkt, addr_map[cpu_addr_aligned], isTiming);
        }
    } else {
        // Received a response packet from memory side

        // Find the original packet
        auto got = reverse_addr_map.find(pkt);
        panic_if(got == reverse_addr_map.end(),
                 "No original packet for addr 0x%x", pkt->getAddr());

        PacketPtr origPkt = got->second;

        // Copy over the response data
        updateOldPacket(origPkt, pkt);

        // Clean up
        reverse_addr_map.erase(got);
        delete pkt;

        return origPkt;
    }
}

// req_type: "timing" | "functional" | "atomic"
bool
CXLcontroller::handleRequest(PacketPtr pkt, std::string req_type)
{
    // Recreate pkt with the right strategy
    PacketPtr newPkt;
    if (cxl_redirect_strategy == "direct") {
        newPkt = handleDirect(pkt);
    } else if (cxl_redirect_strategy == "random") {
        newPkt = handleRandom(pkt, true, req_type == "timing");
    } else if (cxl_redirect_strategy == "speed") {
        newPkt = handleSpeed(pkt, true, req_type == "timing");
    } else {
        panic("Invalid cxl controller redirect strategy. Must be 'direct' | "
              "'random' | 'speed'");
    }

    // Handle different request types
    if (req_type == "functional") {
        mem_port.sendFunctional(newPkt);
    } else if (req_type == "atomic") {
        mem_port.sendAtomic(newPkt);
    } else if (req_type == "timing") {

        // Forward pkt to memory pool
        if (!mem_port.sendTimingReq(newPkt)) {
            mem_port.blocked_packets.push_back(newPkt);
        }

    } else {
        panic("Incorrect request type. Valid types are: 'timing' | "
              "'functional' | 'atomic' ");
    }

    return true;
}

bool
CXLcontroller::handleResponse(PacketPtr pkt)
{
    PacketPtr newPkt;
    if (cxl_redirect_strategy == "direct") {
        newPkt = handleDirect(pkt);
    } else if (cxl_redirect_strategy == "random") {
        newPkt = handleRandom(pkt, false, false);
    } else if (cxl_redirect_strategy == "speed") {
        newPkt = handleSpeed(pkt, false, false);
    } else {
        panic("Invalid cxl controller redirect strategy. Must be 'direct' | "
              "'random' | 'speed'");
    }

    // Forward pkt to cpu
    if (!cpu_port.sendTimingResp(newPkt)) {
        cpu_port.blocked_packets.push_back(newPkt);
    }
    return true;
}

// Port function declarations!

///////////////////////
///// CpuSidePort /////
///////////////////////
AddrRangeList
CXLcontroller::CpuSidePort::getAddrRanges() const
{ return owner->mem_port.getAddrRanges(); }

void
CXLcontroller::CpuSidePort::recvFunctional(PacketPtr pkt)
{ assert(owner->handleRequest(pkt, "functional")); }

Tick
CXLcontroller::CpuSidePort::recvAtomic(PacketPtr pkt)
{
    assert(owner->handleRequest(pkt, "atomic"));
    // TODO: For now just returns 0 instead of an actual Tick
    return 0;
}

bool
CXLcontroller::CpuSidePort::recvTimingReq(PacketPtr pkt)
{
    return owner->handleRequest(pkt, "timing"); // Always returns true
}

void
CXLcontroller::CpuSidePort::recvRespRetry()
{
    panic_if(blocked_packets.empty(),
             "Should never receive retry if doesn't have blocked packets");

    PacketPtr pkt = blocked_packets.front();

    // Forward to CPU
    if (sendTimingResp(pkt)) {
        blocked_packets.pop_front();
    }
}

///// MemSidePort /////
bool
CXLcontroller::MemSidePort::recvTimingResp(PacketPtr pkt)
{
    assert(owner->handleResponse(pkt));
    return true;
}

void
CXLcontroller::MemSidePort::recvReqRetry()
{
    panic_if(blocked_packets.empty(),
             "Should never receive retry if doesn't have blocked packets");

    PacketPtr pkt = blocked_packets.front();

    // Forward to Memory
    if (sendTimingReq(pkt)) {
        blocked_packets.pop_front();
    }
}

void
CXLcontroller::MemSidePort::recvRangeChange()
{ owner->cpu_port.sendRangeChange(); }

}; // namespace gem5

gem5::CXLcontroller *
gem5::CXLcontrollerParams::create() const
{ return new gem5::CXLcontroller(this); }
