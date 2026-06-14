#include <cassert>
#include <cstdlib> // rand() function
#include <deque>
#include <unordered_map>
#include <vector>

#include "mem/packet.hh"
#include "mem/port.hh"
#include "params/CXLcontroller.hh" // Auto generated from src / mem / CXLcontroller.py
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
        std::deque<PacketPtr> blocked_packets;
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
        std::deque<PacketPtr> blocked_packets;
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

    // Config file params
    std::vector<AddrRange> device_addr_ranges;
    std::string cxl_redirect_strategy; // "direct" | "random" | "speed"

    // Helper functions for redirection strategies
    PacketPtr handleDirect(PacketPtr pkt);
    PacketPtr handleRandom(PacketPtr pkt, bool from_cpu, bool isTiming);
    PacketPtr handleSpeed(PacketPtr pkt, bool from_cpu, bool isTiming);
    PacketPtr remapPacket(PacketPtr pkt, Addr new_addr_block, bool isTiming);
    void updateOldPacket(PacketPtr oldPkt, PacketPtr newPkt);

    // Address mappings
    // std::unordered_map<Addr, Addr> addr_map; // Phys addr -> Device addr
    Addr *addr_map; // addr_map[phys_addr_idx] = device addr
    std::unordered_map<PacketPtr, PacketPtr>
        reverse_addr_map; // Device pkt -> Original pkt

    // Pointer to the next free block of every device
    std::vector<Addr> device_next_block;

    // Index of the fastest memory device in CXL memory pool (Assumes 0)
    int speed_device_idx;
    // -----------------------------------------------------------------

  public:
    /** constructor
     */
    CXLcontroller(const CXLcontrollerParams *params);
    Port &getPort(const std::string &if_name,
                  PortID idx = InvalidPortID) override;
    // these functions will do the main work when a packet is received
    bool handleRequest(PacketPtr pkt, std::string req_type);
    bool handleResponse(PacketPtr pkt);
};

}; // namespace gem5
