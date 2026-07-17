#include <cassert>
#include <cstdint>
#include <cstdlib> // rand() function
#include <deque>
#include <unordered_map>
#include <utility>
#include <vector>

#include "mem/packet.hh"
#include "mem/port.hh"
#include "params/CXLcontroller.hh" // Auto generated from "src/mem/CXLcontroller.py"
#include "sim/eventq.hh"
#include "sim/sim_object.hh"

namespace gem5
{
class CXLcontroller : public SimObject
{
  private:
    class CpuSidePort : public ResponsePort
    {
      private:
        CXLcontroller *owner;

      public:
        CpuSidePort(const std::string &name, CXLcontroller *owner)
            : ResponsePort(name), owner(owner)
        {}
        AddrRangeList getAddrRanges() const override;

      protected:
        Tick recvAtomic(PacketPtr pkt) override;
        void recvFunctional(PacketPtr pkt) override;
        bool recvTimingReq(PacketPtr pkt) override;
        void recvRespRetry() override;
    };

    class MemSidePort : public RequestPort
    {
      private:
        CXLcontroller *owner;

      public:
        MemSidePort(const std::string &name, CXLcontroller *owner)
            : RequestPort(name), owner(owner)
        {}

      protected:
        bool recvTimingResp(PacketPtr pkt) override;
        void recvReqRetry() override;
        void recvRangeChange() override;
    };

    CpuSidePort cpu_port;
    MemSidePort mem_port;

    // TODO: My custom data structures, consts, params and helper functions
    // -----------------------------------------------------------------
    // Constants (Addr <=> uint64_t)
    static constexpr Addr BLOCK_SIZE = 64;
    // Granularity of the fragmentation map for "direct"
    // NOTE: Change here (Try page size? 4KiB?)
    static constexpr Addr FRAG_GRANULE = 256;

    // Config file params
    std::vector<AddrRange> device_addr_ranges;
    std::string cxl_redirect_strategy; // "direct" | "random" | "speed"
    int frag_perc;      // % of granules to shuffle ("direct" only, 0 = off)
    uint64_t frag_seed; // Seed for the fragmentation shuffle

    // CXL controller round-trip latency
    // (half is applied per direction: latency / 2)
    const Tick latency;

    // Timing packets to be sent -- needed for latency (event scheduling)
    // Also works as blocked packets queue
    std::deque<std::pair<Tick, PacketPtr>> req_queue;  // To memory
    std::deque<std::pair<Tick, PacketPtr>> resp_queue; // To cpu

    // True after a failed send
    bool req_waiting_retry;
    bool resp_waiting_retry;

    EventFunctionWrapper send_req_event;
    EventFunctionWrapper send_resp_event;

    // Send the front packet of the queue if the peer can take it
    void trySendReq();
    void trySendResp();

    // Helper functions for redirection strategies
    PacketPtr handleDirect(PacketPtr pkt, bool from_cpu, bool isTiming);
    PacketPtr handleRandom(PacketPtr pkt, bool from_cpu, bool isTiming);
    PacketPtr handleSpeed(PacketPtr pkt, bool from_cpu, bool isTiming);
    PacketPtr remapPacket(PacketPtr pkt, Addr new_addr, bool isTiming);
    void updateOldPacket(PacketPtr oldPkt, PacketPtr newPkt);

    // Map a granularity-aligned physical address to an addr map idx
    // NOTE: (Collapses I/O gap for x86 board)
    Addr physToMapIndex(Addr phys_addr, Addr granularity) const;

    // Inverse of physToMapIndex (addr map idx -> aligned physical address)
    Addr mapIndexToPhys(Addr map_idx, Addr granularity) const;

    // Build the fragmentation map for the "direct" strategy
    void initFragMap(Addr total_size);

    // Address mappings
    // std::unordered_map<Addr, Addr> addr_map; // Phys addr -> Device addr
    Addr *addr_map; // addr_map[phys_addr_idx] = device addr
    // Static fragmentation map for "direct" (granule idx -> device addr)
    std::vector<Addr> frag_map;
    std::unordered_map<PacketPtr, PacketPtr>
        reverse_addr_map; // Device pkt -> Original pkt

    // Pointer to the next free block of every device
    std::vector<Addr> device_next_block;

    // Index of the fastest memory device in CXL memory pool (0 by default)
    int speed_device_idx;
    // -----------------------------------------------------------------

  public:
    /** constructor
     */
    CXLcontroller(const CXLcontrollerParams &params);
    Port &getPort(const std::string &if_name,
                  PortID idx = InvalidPortID) override;
    // these functions will do the main work when a packet is received
    // Returns latency in Ticks for "atomic" mode
    Tick handleRequest(PacketPtr pkt, std::string req_type);
    bool handleResponse(PacketPtr pkt);
};

}; // namespace gem5
