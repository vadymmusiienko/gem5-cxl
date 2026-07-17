#include "mem/cxl_controller.hh"

#include <algorithm> // std::shuffle
#include <cstdint>
#include <random>

#include "base/addr_range.hh"
#include "base/logging.hh"
#include "mem/packet.hh"

namespace gem5
{

CXLcontroller::CXLcontroller(const CXLcontrollerParams &params)
    : SimObject(params),
      cpu_port(params.name + ".cpu_port", this),
      mem_port(params.name + ".mem_port", this),
      device_addr_ranges(params.device_ranges),
      cxl_redirect_strategy(params.cxl_strategy),
      frag_perc(params.frag_perc),
      frag_seed(params.frag_seed),
      latency(params.cxl_latency),
      req_waiting_retry(false),
      resp_waiting_retry(false),
      send_req_event([this] { trySendReq(); }, name() + ".sendReqEvent"),
      send_resp_event([this] { trySendResp(); }, name() + ".sendRespEvent"),
      reverse_addr_map(),  // Orig pkt -> New pkt
      device_next_block(), // Next free block of every device
      speed_device_idx(0)  // The index of the fastest device (0 by default)
{
    // Initialize next block of each device to the start of its range
    Addr total_size = 0;
    for (int i = 0; i < device_addr_ranges.size(); i++) {

        // Sanity check (Ensure valid fragmentation granularity)
        assert(device_addr_ranges[i].start() % FRAG_GRANULE == 0);
        assert(device_addr_ranges[i].size() % FRAG_GRANULE == 0);

        total_size += device_addr_ranges[i].size();

        Addr start = device_addr_ranges[i].start();
        device_next_block.push_back(start);
    }

    // Initialize address map
    if (cxl_redirect_strategy == "direct") {
        addr_map = nullptr;
        initFragMap(total_size);
    } else {
        size_t num_blocks = total_size / BLOCK_SIZE;
        addr_map = static_cast<Addr *>(malloc(num_blocks * sizeof(Addr)));
        memset(addr_map, 0xFF, num_blocks * sizeof(Addr));
    }
}

// Build the fragmentation map for the "direct" strategy
// Selects a subset of the device addrs granules and shuffles them
// If frag_perc = 100, then fully shuffled blocks of size FRAG_GRANULE
void
CXLcontroller::initFragMap(Addr total_size)
{
    // Sanity check
    assert(frag_perc >= 0 && frag_perc <= 100);
    assert(FRAG_GRANULE % BLOCK_SIZE == 0);

    // No fragmentation - no map needed
    // NOTE: if FRAG_GRANULE == total_size, then also no frag technically
    if (frag_perc == 0) {
        return;
    }

    // granule idx -> granule-aligned device addr
    size_t num_granules = total_size / FRAG_GRANULE;
    frag_map.resize(num_granules);
    for (size_t i = 0; i < num_granules; i++) {
        frag_map[i] = mapIndexToPhys(i, FRAG_GRANULE);
    }

    // Choose granules to shuffle
    std::mt19937_64 gen(frag_seed);
    std::vector<size_t> indices(num_granules); // 0, 1, 2 ...
    for (size_t i = 0; i < num_granules; i++) {
        indices[i] = i;
    }
    std::shuffle(indices.begin(), indices.end(), gen);

    size_t num_shuffled = num_granules * frag_perc / 100;

    // Shuffle the device addrs of the chosen granules among themselves
    std::vector<Addr> chosen_addrs(num_shuffled);
    for (size_t i = 0; i < num_shuffled; i++) {
        chosen_addrs[i] = frag_map[indices[i]];
    }
    std::shuffle(chosen_addrs.begin(), chosen_addrs.end(), gen);
    for (size_t i = 0; i < num_shuffled; i++) {
        frag_map[indices[i]] = chosen_addrs[i];
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

// NOTE: This function handles the I/O hole in X86board
//
// Returns address map index for the current physical address granule
// Takes into account the I/O holde in X86board, collapses the gap between
// device ranges such that [0, total_size / granularity] is enough space
//
// If there is no I/O hole, it will just return phys_addr / granularity
// NOTE: granularity can be BLOCK_SIZE or FRAG_GRANULE
Addr
CXLcontroller::physToMapIndex(Addr phys_addr, Addr granularity) const
{
    Addr taken_space = 0;
    for (int i = 0; i < device_addr_ranges.size(); i++) {
        AddrRange range = device_addr_ranges[i];
        if (range.contains(phys_addr)) {
            return (taken_space + (phys_addr - range.start())) / granularity;
        }
        taken_space += range.size();
    }
    panic("Physical address %#x is not in any device range", phys_addr);
}

// Inverse of physToMapIndex
// Used by direct  method for fragmentation
// Takes frag_map idx and returns physical addr
// granularity == FRAG_GRANULE
Addr
CXLcontroller::mapIndexToPhys(Addr map_idx, Addr granularity) const
{
    Addr addr_offset = map_idx * granularity;
    for (int i = 0; i < device_addr_ranges.size(); i++) {
        AddrRange range = device_addr_ranges[i];

        // Find the correct device
        if (addr_offset < range.size()) {
            return range.start() + addr_offset;
        }
        addr_offset -= range.size();
    }
    panic("Addr map index %d is not in any device range", map_idx);
}

// Copy a packet and update its address
// !! DOES NOT UPDATE device_next_block or the address maps !!
PacketPtr
CXLcontroller::remapPacket(PacketPtr pkt, Addr new_addr, bool isTiming)
{
    // Don't create a new packet for functional and atomic requests
    if (!isTiming) {
        pkt->setAddr(new_addr);
        return pkt;
    }

    // Copy the old packet (keep the flags and create new data buffer)
    PacketPtr newPkt = new Packet(pkt, false, true);
    newPkt->setAddr(new_addr); // Overwrite the address

    // TODO: THIS WAS ANOTHER BUG (I DIDN'T HAVE THIS)
    // Copy over the data
    if (pkt->isWrite()) {
        newPkt->setData(pkt->getConstPtr<uint8_t>());
    }

    // Only add to reverse map if needs response
    if (pkt->needsResponse()) {
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

// Takes in a pkt from cpu or memory and returns an appropriate packet
// Remaps through the fragmentation map
// (if frag_perc == 0 - no fragmentation)
PacketPtr
CXLcontroller::handleDirect(PacketPtr pkt, bool from_cpu, bool isTiming)
{
    // No fragmentation (No work needed)
    if (frag_perc == 0) {
        return pkt;
    }

    if (from_cpu) {
        // Received a request packet from cpu side

        // Alignment (granule)
        Addr cpu_addr = pkt->getAddr();            // Non-aligned phys
        Addr offset = cpu_addr % FRAG_GRANULE;     // == 0 if already aligned
        Addr cpu_addr_aligned = cpu_addr - offset; // Aligned phys

        // TODO: Can a packet span two blocks/granules?
        panic_if(offset + pkt->getSize() > FRAG_GRANULE,
                 "Packet at %#x (size %d) spans two fragmentation granules",
                 cpu_addr, pkt->getSize());

        Addr frag_map_idx = physToMapIndex(cpu_addr_aligned, FRAG_GRANULE);
        Addr device_addr_aligned = frag_map[frag_map_idx];

        // Remap the packet
        return remapPacket(pkt, device_addr_aligned + offset, isTiming);
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

        assert(cpu_addr_aligned % BLOCK_SIZE == 0);
        Addr addr_map_idx =
            physToMapIndex(cpu_addr_aligned, BLOCK_SIZE); // addr map array idx

        // TODO: -1 or should i use 0xFF
        if (addr_map[addr_map_idx] == (Addr)-1) {
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

            // Phys addr -> Device addr (Block aligned)
            addr_map[addr_map_idx] = next_block;

            // Remap the packet
            PacketPtr newPkt = remapPacket(pkt, next_block + offset, isTiming);

            // Update next free block for this device
            device_next_block[device_idx] += BLOCK_SIZE;

            return newPkt;

        } else {
            // Already mapped (exists)
            return remapPacket(pkt, addr_map[addr_map_idx] + offset, isTiming);
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

        assert(cpu_addr_aligned % BLOCK_SIZE == 0);
        Addr addr_map_idx =
            physToMapIndex(cpu_addr_aligned, BLOCK_SIZE); // addr map array idx

        // TODO: -1 or 0xFF
        if (addr_map[addr_map_idx] == (Addr)-1) {
            // First time seeing this address

            // Choose fastest device
            assert(speed_device_idx < device_addr_ranges.size());
            AddrRange range = device_addr_ranges[speed_device_idx];
            Addr next_block = device_next_block[speed_device_idx];
            assert(range.end() != next_block);

            // Phys addr -> Device addr (Block aligned)
            addr_map[addr_map_idx] = next_block;

            // Remap the packet
            PacketPtr newPkt = remapPacket(pkt, next_block + offset, isTiming);

            // Update next free block for this device
            device_next_block[speed_device_idx] += BLOCK_SIZE;

            // Update speed memory index
            if (device_next_block[speed_device_idx] == range.end()) {
                speed_device_idx += 1;
                panic_if(speed_device_idx >= device_next_block.size(),
                         "Ran out of memory across all CXL memory devices");
            }

            return newPkt;

        } else {
            // Already mapped (exists)
            return remapPacket(pkt, addr_map[addr_map_idx] + offset, isTiming);
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

// Send the front packet of req_queue to memory
void
CXLcontroller::trySendReq()
{
    assert(!req_queue.empty());

    PacketPtr pkt = req_queue.front().second;
    if (!mem_port.sendTimingReq(pkt)) {
        // Busy, recvReqRetry will be called
        req_waiting_retry = true;
        return;
    }
    req_waiting_retry = false;
    req_queue.pop_front();

    // Schedule the next packet
    // Might be past its ready tick (if was blocked)
    if (!req_queue.empty()) {
        schedule(send_req_event, std::max(curTick(), req_queue.front().first));
    }
}

// Send the front packet of res_queue to cpu
void
CXLcontroller::trySendResp()
{
    assert(!resp_queue.empty());

    PacketPtr pkt = resp_queue.front().second;
    if (!cpu_port.sendTimingResp(pkt)) {
        // Busy, recvRespRetry will be called
        resp_waiting_retry = true;
        return;
    }
    resp_waiting_retry = false;
    resp_queue.pop_front();

    if (!resp_queue.empty()) {
        schedule(send_resp_event,
                 std::max(curTick(), resp_queue.front().first));
    }
}

// req_type: "timing" | "functional" | "atomic"
Tick
CXLcontroller::handleRequest(PacketPtr pkt, std::string req_type)
{
    // Recreate pkt with the right strategy
    PacketPtr newPkt;
    if (cxl_redirect_strategy == "direct") {
        newPkt = handleDirect(pkt, true, req_type == "timing");
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
        // Functional accesses are for setting memory, thus no latency
        mem_port.sendFunctional(newPkt);
        return 0;
    } else if (req_type == "atomic") {
        // No events, so return just the added round-trip latency?
        return mem_port.sendAtomic(newPkt) + latency;
    } else if (req_type == "timing") {

        // Queue the pkt
        // It will leave the controller at the "ready" tick
        // (one direction = half the round-trip latency)
        Tick ready_at = curTick() + latency / 2;
        req_queue.emplace_back(ready_at, newPkt);
        if (!send_req_event.scheduled() && !req_waiting_retry) {
            schedule(send_req_event, ready_at);
        }
        return 0;

    } else {
        panic("Incorrect request type. Valid types are: 'timing' | "
              "'functional' | 'atomic' ");
    }
}

bool
CXLcontroller::handleResponse(PacketPtr pkt)
{
    PacketPtr newPkt;
    if (cxl_redirect_strategy == "direct") {
        newPkt = handleDirect(pkt, false, false);
    } else if (cxl_redirect_strategy == "random") {
        newPkt = handleRandom(pkt, false, false);
    } else if (cxl_redirect_strategy == "speed") {
        newPkt = handleSpeed(pkt, false, false);
    } else {
        panic("Invalid cxl controller redirect strategy. Must be 'direct' | "
              "'random' | 'speed'");
    }

    // Queue the pkt for the cpu; it may leave the controller once the
    // latency has elapsed (one direction = half the round-trip latency)
    Tick ready_at = curTick() + latency / 2;
    resp_queue.emplace_back(ready_at, newPkt);
    if (!send_resp_event.scheduled() && !resp_waiting_retry) {
        schedule(send_resp_event, ready_at);
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
{ owner->handleRequest(pkt, "functional"); }

Tick
CXLcontroller::CpuSidePort::recvAtomic(PacketPtr pkt)
{ return owner->handleRequest(pkt, "atomic"); }

bool
CXLcontroller::CpuSidePort::recvTimingReq(PacketPtr pkt)
{
    owner->handleRequest(pkt, "timing");
    return true; // Never blocks (unbounded queue)
}

void
CXLcontroller::CpuSidePort::recvRespRetry()
{
    panic_if(!owner->resp_waiting_retry,
             "Should never receive retry without a blocked packet");
    owner->trySendResp();
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
    panic_if(!owner->req_waiting_retry,
             "Should never receive retry without a blocked packet");
    owner->trySendReq();
}

void
CXLcontroller::MemSidePort::recvRangeChange()
{ owner->cpu_port.sendRangeChange(); }

}; // namespace gem5
