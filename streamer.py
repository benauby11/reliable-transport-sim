# do not import anything else from loss_socket besides LossyUDP
from lossy_socket import LossyUDP
# do not import anything else from socket except INADDR_ANY
from socket import INADDR_ANY
import struct
import concurrent.futures
import time
import hashlib
import threading

UDP_size = 1472
PACK_FORMAT = 'ii??16s'
header_size = struct.calcsize(PACK_FORMAT)
max_payload_size = UDP_size - header_size
timeout = 0.25

class Streamer:
    def __init__(self, dst_ip, dst_port,
                 src_ip=INADDR_ANY, src_port=0):
        """Default values listen on all network interfaces, chooses a random source port,
           and does not introduce any simulated packet loss."""
        self.socket = LossyUDP()
        self.socket.bind((src_ip, src_port))
        self.dst_ip = dst_ip
        self.dst_port = dst_port
        self.send_num = 0
        self.rec_num = 0
        self.acks_sent = 0
        self.last_acked = -1
        self.rec_buf = {}
        self.send_buf = {}
        self.closed = False
        self.ack = 0
        self.fin_ack = False
        self.expires = time.perf_counter() + 0.25
        self.lock = threading.Lock()
        self.timer = None
        self.got_fin = False
        executor = concurrent.futures.ThreadPoolExecutor(max_workers=1)
        executor.submit(self.listener)

    # we wanna start a timer thread, stop the old if already running
    def start_timer(self):
        if self.timer:
            self.timer.cancel()
        # we'll pass "resend" which resends all the packets we don't get an ACK for
        self.timer = threading.Timer(timeout, self.resend)
        self.timer.start()

    # resend all packets we didn't get
    def resend(self):
        if self.closed:
            return

        # lock so we don't get the iterable changes error or whatever
        self.lock.acquire()
        # send them all again if we don't have an ACK
        for packet in self.send_buf.values():
            self.socket.sendto(packet, (self.dst_ip, self.dst_port))

        # if there's still stuff left in the send buffer, start the timer again
        if self.send_buf:
            self.start_timer()

        # 1, 2, 1, 2, 3 release em
        self.lock.release()

    # thread for listening for new packets
    def listener(self):
        while not self.closed:
            try:
                # try receiving data
                data, addr = self.socket.recvfrom()
                # if there's no data to receive, we continue
                if not data:
                    continue
                # if there is data, then we unpack it
                header = struct.unpack(PACK_FORMAT, data[:header_size])
                data = data[header_size:]
                seq_num = int(header[0])
                payload_size = int(header[1])
                payload = data[:payload_size]
                is_ack = int(header[2]) == 1
                is_fin = int(header[3]) == 1
                hash = header[4]
                #print("Received packet " + str(seq_num))
                header_no_hash = struct.pack('ii??', seq_num, payload_size, is_ack, is_fin)
                if header[4] != hashlib.md5(header_no_hash + payload).digest():
                    continue


                # we have an acknowledgement for DATA, so sender is receiving
                if is_ack and is_fin == False:
                    # we want the packet that we last acked
                    self.last_acked = max(self.last_acked, seq_num)
                    i = self.last_acked
                    # Remove acknowledged packets from send buffer
                    self.lock.acquire()
                    for j in list(self.send_buf.keys()):
                        if j <= i:
                            self.send_buf.pop(j)
                    if not self.send_buf:
                        if self.timer:
                            self.timer.cancel()

                    self.lock.release()
                # we have a FIN ACK
                elif is_ack and is_fin:
                    self.fin_ack = True
                elif is_ack:
                    continue
                elif is_fin:
                    #print("Sending fin ack")
                    header_to_hash = struct.pack("ii??", seq_num, 0, 1, 1)
                    hash_val = hashlib.md5(header_to_hash).digest()
                    finack_header = struct.pack(PACK_FORMAT, seq_num, 0, 1, 1, hash_val)
                    self.got_fin = True
                    self.socket.sendto(finack_header, (self.dst_ip, self.dst_port))
                else:
                    # we have received a packet, and now need to ack it
                    ack_num = self.rec_num
                    #print(seq_num)
                    #print(self.send_num)
                    if seq_num == self.rec_num:
                        #print("sending ACK " + str(seq_num))
                        header_to_hash = struct.pack("ii??", seq_num, 0, 1, 0)
                        hash_val = hashlib.md5(header_to_hash).digest()
                        ack_header = struct.pack(PACK_FORMAT, seq_num, 0, 1, 0, hash_val)
                        self.socket.sendto(ack_header, (self.dst_ip, self.dst_port))
                        payload = data[:payload_size]
                        self.rec_buf[seq_num] = payload
                        #self.rec_num += 1
                    else:
                        #print("REsending ACK " + str(ack_num))
                        header_to_hash = struct.pack("ii??", ack_num - 1, 0, 1, 0)
                        hash_val = hashlib.md5(header_to_hash).digest()
                        ack_header = struct.pack(PACK_FORMAT, ack_num - 1, 0, 1, 0, hash_val)
                        self.socket.sendto(ack_header, (self.dst_ip, self.dst_port))
                        payload = data[:payload_size]
                        self.rec_buf[seq_num] = payload
            except Exception as e:
                print("listener died!")
                print(e)

    def send(self, data_bytes: bytes) -> None:
        total_len = len(data_bytes)

        while total_len > 0:
            payload_size = min(total_len, max_payload_size)
            if payload_size == 0:
                payload_size = max_payload_size

            header_to_hash = struct.pack("ii??", self.send_num, payload_size, 0, 0)
            hash_val = hashlib.md5(header_to_hash + data_bytes[:payload_size]).digest()
            header = struct.pack(PACK_FORMAT, self.send_num, payload_size, 0, 0, hash_val)
            packet = header + data_bytes[:payload_size]
            self.lock.acquire()
            self.send_buf[self.send_num] = packet
            self.send_num += 1
            self.socket.sendto(packet, (self.dst_ip, self.dst_port))
            # we gotta start the timer to wait to get ACKs
            if len(self.send_buf) == 1:
                self.start_timer()
            self.lock.release()
            self.expires = time.perf_counter() + 0.25
            send_time = time.perf_counter()
            #print("ACK received")
            data_bytes = data_bytes[payload_size:]
            total_len -= payload_size

    def recv(self) -> bytes:
        """Blocks (waits) if no data is ready to be read from the connection."""
        to_return = b''
        while self.rec_buf.get(self.rec_num) is None:
            time.sleep(0.01)
        to_return = to_return + self.rec_buf[self.rec_num]
        self.rec_buf.pop(self.rec_num)
        self.rec_num += 1
        return to_return

    def close(self) -> None:
        """Cleans up. It should block (wait) until the Streamer is done with all
           the necessary ACKs and retransmissions"""

        # wait until we have all ACKs
        while (self.send_num - 1) != self.last_acked:
            time.sleep(0.01)

        # hash the header
        header_to_hash = struct.pack("ii??", self.send_num, 0, 0, 1)
        hash_val = hashlib.md5(header_to_hash).digest()
        fin_header = struct.pack(PACK_FORMAT, self.send_num, 0, 0, 1, hash_val)
        self.socket.sendto(fin_header, (self.dst_ip, self.dst_port))
        send_time = time.perf_counter()
        while not self.fin_ack:
            if time.perf_counter() - 0.25 >= send_time:
                self.socket.sendto(fin_header, (self.dst_ip, self.dst_port))
                send_time = time.perf_counter()
                continue
            time.sleep(0.01)
        #print("GOT FIN ACK")

        # now, we gotta wait for the fin to come in from the other before closing
        while not self.got_fin:
            time.sleep(0.01)
        self.fin_ack = False
        time.sleep(2)
        self.closed = True
        self.socket.stoprecv()
