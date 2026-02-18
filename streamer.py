# do not import anything else from loss_socket besides LossyUDP
from lossy_socket import LossyUDP
# do not import anything else from socket except INADDR_ANY
from socket import INADDR_ANY
import struct
import concurrent.futures
import time
import hashlib
import threading

UDP_SIZE = 1472
PREHASH_FORMAT = 'ii??'
PACK_FORMAT = PREHASH_FORMAT + '16s' # includes hash
HEADER_SIZE = struct.calcsize(PACK_FORMAT)
MAX_PAYLOAD_SIZE = UDP_SIZE - HEADER_SIZE
TIMEOUT = 0.1

class Packet:
    def __init__(self, seq_num = 0, payload_size = 0, is_ack = 0, is_fin = 0, payload = b'', raw_data = b''):
        self.seq_num = seq_num
        self.is_ack = is_ack
        self.is_fin = is_fin
        self.payload = payload
        self.payload_size = payload_size
        prehash = struct.pack(PREHASH_FORMAT, self.seq_num, self.payload_size, self.is_ack, self.is_fin)
        self.hashed = hashlib.md5(prehash + self.payload).digest()
        self.to_send = (struct.pack(PACK_FORMAT,
                                    self.seq_num,
                                    self.payload_size,
                                    self.is_ack,
                                    self.is_fin,
                                    self.hashed)
                        + self.payload)
        if raw_data: # unpack the raw data to populate fields
            header = struct.unpack(PACK_FORMAT, raw_data[:HEADER_SIZE])
            self.seq_num = header[0]
            self.payload_size = header[1]
            self.is_ack = header[2]
            self.is_fin = header[3]
            self.hashed = header[4]
            self.payload = raw_data[HEADER_SIZE:]
            self.to_send = raw_data

    # return True if the packet has been corrupted
    def is_corrupted(self):
        prehash = struct.pack(PREHASH_FORMAT, self.seq_num, self.payload_size, self.is_ack, self.is_fin)
        return self.hashed != hashlib.md5(prehash + self.payload).digest()

class Streamer:
    def __init__(self, dst_ip, dst_port,
                 src_ip=INADDR_ANY, src_port=0):
        """Default values listen on all network interfaces, chooses a random source port,
           and does not introduce any simulated packet loss."""
        self.socket = LossyUDP()
        self.socket.bind((src_ip, src_port))
        self.dst_ip = dst_ip
        self.dst_port = dst_port

        self.next_to_send = 0
        self.next_to_receive = 0
        self.last_acked = -1

        self.rec_buf = {}
        self.resend_buf = {}

        self.closed = False
        self.fin_ack = False
        self.fin = False

        self.expires = time.perf_counter() + TIMEOUT

        self.lock = threading.Lock()
        executor = concurrent.futures.ThreadPoolExecutor(max_workers=2)
        executor.submit(self.listener_thread)
        executor.submit(self.resend_thread)

    def resend_thread(self):
        while not self.closed:
            try:
                # if timeout occurs, resend packets that have not been acknowledged
                if time.perf_counter() >= self.expires:
                    self.lock.acquire()
                    # lock so that shared buffer is not modified
                    for packet in list(self.resend_buf.values()):
                        self.socket.sendto(packet.to_send, (self.dst_ip, self.dst_port))
                    self.expires = time.perf_counter() + TIMEOUT
                    self.lock.release()
            except Exception as e:
                print("Sender died!")
                print(e)

    def listener_thread(self):
        while not self.closed:
            try:
                data, addr = self.socket.recvfrom()
                self.lock.acquire()
                if not data: # ignore
                    self.lock.release()
                    continue
                packet = Packet(raw_data=data)
                # drop corrupted packets
                if packet.is_corrupted():
                    self.lock.release()
                    continue

                if packet.is_fin and packet.is_ack:
                    self.fin_ack = True
                elif packet.is_fin: # send fin ack
                    self.fin = True
                    fin_ack = Packet(is_ack=1, is_fin=1)
                    self.socket.sendto(fin_ack.to_send, (self.dst_ip, self.dst_port))
                elif packet.is_ack: # everything up to the ack seq # has been received
                    i = self.last_acked + 1
                    self.last_acked = max(self.last_acked, packet.seq_num)
                    # Remove acknowledged packets from send buffer
                    while i <= self.last_acked:
                        self.resend_buf.pop(i)
                        i += 1
                else: # add to receive buffer
                    self.rec_buf[packet.seq_num] = packet
                self.lock.release()
            except Exception as e:
                print("listener died!")
                print(e)

    def send(self, data_bytes: bytes) -> None:
        total_len = len(data_bytes)
        while total_len > 0:
            payload_size = min(MAX_PAYLOAD_SIZE, total_len)
            packet = Packet(seq_num=self.next_to_send, payload_size=payload_size, payload=data_bytes[:payload_size])
            self.lock.acquire()
            self.resend_buf[self.next_to_send] = packet
            self.next_to_send += 1
            self.lock.release()
            self.socket.sendto(packet.to_send, (self.dst_ip, self.dst_port))
            data_bytes = data_bytes[payload_size:]
            total_len -= payload_size

    def recv(self) -> bytes:
        while self.rec_buf.get(self.next_to_receive) is None:
            time.sleep(0.01)
        # receive everything contiguous and send acknowledgements
        self.lock.acquire()
        to_return = b''
        while self.rec_buf.get(self.next_to_receive) is not None:
            ack = Packet(seq_num=self.next_to_receive, is_ack=1)
            self.socket.sendto(ack.to_send, (self.dst_ip, self.dst_port))
            to_return = to_return + (self.rec_buf.get(self.next_to_receive)).payload
            self.next_to_receive += 1
        # send acknowledgements for non-contiguous packets
        for out_of_order in list(self.rec_buf.keys()):
            ack = Packet(seq_num=self.next_to_receive - 1, is_ack=1)
            self.socket.sendto(ack.to_send, (self.dst_ip, self.dst_port))
        self.rec_buf = {}
        self.lock.release()
        return to_return

    def close(self) -> None:
        """Cleans up. It should block (wait) until the Streamer is done with all
           the necessary ACKs and retransmissions"""
        # your code goes here, especially after you add ACKs and retransmissions.
        while self.last_acked != self.next_to_send - 1:
            time.sleep(0.01)
        fin = Packet(is_fin=1)
        self.socket.sendto(fin.to_send, (self.dst_ip, self.dst_port))
        send_time = time.perf_counter()
        while not (self.fin_ack or self.fin):
            if time.perf_counter() - 0.25 >= send_time:
                self.socket.sendto(fin.to_send, (self.dst_ip, self.dst_port))
                send_time = time.perf_counter()
                continue
            time.sleep(0.01)
        time.sleep(2)
        self.closed = True
        self.socket.stoprecv()
