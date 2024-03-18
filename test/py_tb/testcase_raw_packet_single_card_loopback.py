from mock_host import *

from scapy.compat import raw, Tuple
from scapy.error import warning
from scapy.fields import (
    BitEnumField,
    ByteEnumField,
    ByteField,
    XByteField,
    ShortField,
    XShortField,
    XIntField,
    XLongField,
    BitField,
    XBitField,
    FCSField,
)
from scapy.layers.inet import IP, UDP
from scapy.layers.l2 import Ether
from scapy.packet import Packet, bind_layers, Raw
from zlib import crc32

TOTAL_MEMORY_SIZE = 1024 * 1024 * 64
PGT_ENTRY_OFFSET = 0x200
PGT_ENTRY_CNT = 0x20
PGT_ENTRY_SIZE = 0x08
# PGT_MR0_BASE_VA = 0xFBABCDCEEEEE0001
PGT_MR0_BASE_VA = 0x0000000000000000

CMD_QUEUE_H2C_RINGBUF_START_PA = 0x00
CMD_QUEUE_C2H_RINGBUF_START_PA = 0x1000
SEND_QUEUE_RINGBUF_START_PA = 0x2000
META_REPORT_QUEUE_RINGBUF_START_PA = 0x3000

PGT_TABLE_START_PA_IN_HOST_MEM = 0x10000

HUGEPAGE_2M_ADDR_MASK = 0xFFFFFFFFFFE00000
HUGEPAGE_2M_BYTE_CNT = 0x200000

# MR_0_PA_START = 0x100000
MR_0_PA_START = 0x0

MR_0_PTE_COUNT = 0x20
MR_0_LENGTH = MR_0_PTE_COUNT * HUGEPAGE_2M_BYTE_CNT
print("MR_0_LENGTH=", hex(MR_0_LENGTH))

print("PGT_MR0_BASE_VA=", hex(PGT_MR0_BASE_VA))
# REQ_SIDE_VA_ADDR = (PGT_MR0_BASE_VA & HUGEPAGE_2M_ADDR_MASK) + 0x1FFFFE
REQ_SIDE_VA_ADDR = (PGT_MR0_BASE_VA & HUGEPAGE_2M_ADDR_MASK) + 0x200000
print("REQ_SIDE_VA_ADDR=", hex(REQ_SIDE_VA_ADDR))
RESP_SIDE_VA_ADDR = (PGT_MR0_BASE_VA & HUGEPAGE_2M_ADDR_MASK) + 0x90000
print("RESP_SIDE_VA_ADDR=", hex(RESP_SIDE_VA_ADDR))

SEND_SIDE_KEY = 0x6622
RECV_SIDE_KEY = 0x6622
PKEY_INDEX = 0

SEND_SIDE_QPN = 0x6611
SEND_SIDE_PD_HANDLER = 0x6611  # in practise, this should be returned by hardware

PMTU_VALUE_FOR_TEST = PMTU.IBV_MTU_4096

RECV_SIDE_IP = 0x11223344
RECE_SIDE_MAC = 0xAABBCCDDEEFF
RECV_SIDE_QPN = 0x6611
SEND_SIDE_PSN = 0x22

SEND_BYTE_COUNT = 1024*16


def test_case():
    host_mem = MockHostMem("/bluesim1", TOTAL_MEMORY_SIZE)
    mock_nic = MockNicAndHost(host_mem)
    MockNicAndHost.do_self_loopback(mock_nic)
    mock_nic.run()

    cmd_req_queue = RingbufCommandReqQueue(
        host_mem, CMD_QUEUE_H2C_RINGBUF_START_PA, mock_host=mock_nic)
    cmd_resp_queue = RingbufCommandRespQueue(
        host_mem, CMD_QUEUE_C2H_RINGBUF_START_PA, mock_host=mock_nic)
    send_queue = RingbufSendQueue(
        host_mem, SEND_QUEUE_RINGBUF_START_PA, mock_host=mock_nic)
    meta_report_queue = RingbufMetaReportQueue(
        host_mem, META_REPORT_QUEUE_RINGBUF_START_PA, mock_host=mock_nic)

    cmd_req_queue.put_desc_update_mr_table(
        base_va=PGT_MR0_BASE_VA,
        length=MR_0_LENGTH,
        key=SEND_SIDE_KEY,
        pd_handle=SEND_SIDE_PD_HANDLER,
        pgt_offset=PGT_ENTRY_OFFSET,
        acc_flag=MemAccessTypeFlag.IBV_ACCESS_LOCAL_WRITE | MemAccessTypeFlag.IBV_ACCESS_REMOTE_READ | MemAccessTypeFlag.IBV_ACCESS_REMOTE_WRITE,
    )

    cmd_req_queue.put_desc_update_pgt(
        dma_addr=PGT_TABLE_START_PA_IN_HOST_MEM,
        dma_length=PGT_ENTRY_CNT * PGT_ENTRY_SIZE,
        start_index=PGT_ENTRY_OFFSET,
    )

    cmd_req_queue.put_desc_update_qp(
        qpn=SEND_SIDE_QPN,
        pd_handler=SEND_SIDE_PD_HANDLER,
        qp_type=TypeQP.IBV_QPT_RC,
        acc_flag=MemAccessTypeFlag.IBV_ACCESS_LOCAL_WRITE | MemAccessTypeFlag.IBV_ACCESS_REMOTE_READ | MemAccessTypeFlag.IBV_ACCESS_REMOTE_WRITE,
        pmtu=PMTU_VALUE_FOR_TEST,
    )

    # generate second level PGT entry
    PgtEntries = c_longlong * MR_0_PTE_COUNT
    entries = PgtEntries()

    for i in range(len(entries)):
        entries[i] = MR_0_PA_START + i * HUGEPAGE_2M_BYTE_CNT

    bytes_to_copy = bytes(entries)
    host_mem.buf[PGT_TABLE_START_PA_IN_HOST_MEM:PGT_TABLE_START_PA_IN_HOST_MEM +
                 len(bytes_to_copy)] = bytes_to_copy

    # ring doorbell
    cmd_req_queue.sync_pointers()

    # read cmd resp queue head pointer to check if all cmd executed
    for _ in range(3):
        cmd_resp_queue.deq_blocking()

    # generate raw packet data
    ip_layer = IP(dst="17.34.51.68")
    udp_layer = UDP(dport=1111, sport=2222)

    bytes_to_send = bytes(ip_layer/udp_layer/"mmmmmmmmmmmmmmmmmmmmmmmmmmmmm")
    print("bytes_to_send=", bytes_to_send)
    host_mem.buf[REQ_SIDE_VA_ADDR:REQ_SIDE_VA_ADDR +
                 len(bytes_to_send)] = bytes_to_send
    print("src_mem=", bytes(host_mem.buf[REQ_SIDE_VA_ADDR:REQ_SIDE_VA_ADDR +
                                         len(bytes_to_send)]))

    # move send queue head to send WQE
    sgl = [
        SendQueueReqDescFragSGE(
            F_LKEY=SEND_SIDE_KEY, F_LEN=len(bytes_to_send), F_LADDR=REQ_SIDE_VA_ADDR),
    ]
    send_queue.put_work_request(
        opcode=WorkReqOpCode.IBV_WR_RDMA_WRITE_WITH_IMM,
        is_first=True,
        is_last=True,
        sgl=sgl,
        r_va=RESP_SIDE_VA_ADDR,
        r_key=RECV_SIDE_KEY,
        r_ip=RECV_SIDE_IP,
        r_mac=RECE_SIDE_MAC,
        dqpn=RECV_SIDE_QPN,
        psn=SEND_SIDE_PSN,
        pmtu=PMTU_VALUE_FOR_TEST,
        qp_type=TypeQP.IBV_QPT_RAW_PACKET,
    )

    send_queue.sync_pointers()
    for report_idx in range(1):
        meta_report_queue.deq_blocking()
        print("receive meta report: ", report_idx)

    dst_mem = mock_nic.main_memory.buf[RESP_SIDE_VA_ADDR:RESP_SIDE_VA_ADDR + 57]

    print("dst_mem=", bytes(dst_mem))

    eth_header = Ether(dst_mem)
    print(eth_header)

    ip_header = IP(bytes(eth_header.payload))
    print(ip_header)

    # 打印IP包头信息
    print("IP Header:")
    print("Source IP:", ip_header.src)
    print("Destination IP:", ip_header.dst)
    print("Protocol:", ip_header.proto)

    # 获取UDP包头
    udp_header = UDP(bytes(ip_header.payload))

    # 打印UDP包头信息
    print("UDP Header:")
    print("Source Port:", udp_header.sport)
    print("Destination Port:", udp_header.dport)
    print("Length:", udp_header.len)

    udp_payload = bytes(udp_header.payload)
    print("UDP Payload:", udp_payload)

    # if src_mem != dst_mem:
    #     print("Error: DMA Target mem is not the same as source mem")
    #     for idx in range(len(src_mem)):
    #         if src_mem[idx] != dst_mem[idx]:
    #             print("id:", idx,
    #                   "src: ", hex(src_mem[idx]),
    #                   "dst: ", hex(dst_mem[idx])
    #                   )
    # else:
    #     print("PASS")

    mock_nic.stop()


if __name__ == "__main__":
    # must wrap test case in a function, so when the function returned, the memory view will be cleaned
    # otherwise, there will be an warning at program exit.
    test_case()
