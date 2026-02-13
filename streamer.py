# do not import anything else from loss_socket besides LossyUDP
from lossy_socket import LossyUDP
# do not import anything else from socket except INADDR_ANY
from socket import INADDR_ANY
import struct
import concurrent.futures
import time

UDP_size = 1472
header_size = 10
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
        self.rec_buf = {}
        self.closed = False
        self.ack = False
        self.fin_ack = False
        executor = concurrent.futures.ThreadPoolExecutor(max_workers=1)
        executor.submit(self.listener)

    def listener(self):
        while not self.closed:  # a later hint will explain self.closed
            try:
                data, addr = self.socket.recvfrom()
                if not data:
                    continue
                header = struct.unpack('ii??', data[:header_size])
                data = data[header_size:]
                seq_num = int(header[0])
                payload_size = int(header[1])
                is_ack = int(header[2]) == 1
                is_fin = int(header[3]) == 1
                if is_ack and seq_num == self.send_num and is_fin == False:
                    self.ack = True
                elif is_ack and is_fin:
                    self.fin_ack = True
                elif is_fin:
                    fin_header = struct.pack('ii??', seq_num, 0, 1, 1)
                    self.socket.sendto(fin_header, (self.dst_ip, self.dst_port))
                else:
                    if seq_num < self.rec_num:
                        ack_header = struct.pack('ii??', seq_num, 0, 1, 0)
                        self.socket.sendto(ack_header, (self.dst_ip, self.dst_port))
                        continue
                    payload = data[:payload_size]
                    self.rec_buf[seq_num] = payload
            except Exception as e:
                print("listener died!")
                print(e)

    def send(self, data_bytes: bytes) -> None:
        total_len = len(data_bytes)

        while total_len > 0:
            payload_size = total_len % max_payload_size
            if payload_size == 0:
                payload_size = max_payload_size

            header = struct.pack('ii??', self.send_num, payload_size, 0, 0)
            packet = header + data_bytes[:payload_size]
            self.socket.sendto(packet, (self.dst_ip, self.dst_port))
            send_time = time.perf_counter()
            while not self.ack:
                if time.perf_counter() - 0.25 >= send_time:
                    self.socket.sendto(packet, (self.dst_ip, self.dst_port))
                    send_time = time.perf_counter()
                    continue
                time.sleep(0.01)
            print("ACK received")
            self.ack = False
            data_bytes = data_bytes[payload_size:]
            total_len -= payload_size
            self.send_num += 1

    def recv(self) -> bytes:
        """Blocks (waits) if no data is ready to be read from the connection."""
        to_return = b''
        if len(self.rec_buf) >= 1 and self.rec_buf.get(self.rec_num) is not None:
            ack_header = struct.pack('ii??', self.rec_num, 0, 1, 0)
            self.socket.sendto(ack_header, (self.dst_ip, self.dst_port))
            to_return = to_return + self.rec_buf[self.rec_num]
            self.rec_buf.pop(self.rec_num)
            self.rec_num += 1
        return to_return

    def close(self) -> None:
        """Cleans up. It should block (wait) until the Streamer is done with all
           the necessary ACKs and retransmissions"""
        # your code goes here, especially after you add ACKs and retransmissions.
        print("CLOSING")
        fin_header = struct.pack('ii??', self.send_num, 0, 0, 1)
        self.socket.sendto(fin_header, (self.dst_ip, self.dst_port))
        while not self.fin_ack:
            send_time = time.perf_counter()
            if time.perf_counter() - 0.25 >= send_time:
                self.socket.sendto(fin_header, (self.dst_ip, self.dst_port))
                send_time = time.perf_counter()
                continue
            time.sleep(0.01)
        print("GOT FIN ACK")
        self.fin_ack = False
        time.sleep(2)
        self.closed = True
        self.socket.stoprecv()
