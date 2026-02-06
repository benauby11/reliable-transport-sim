# do not import anything else from loss_socket besides LossyUDP
from lossy_socket import LossyUDP
# do not import anything else from socket except INADDR_ANY
from socket import INADDR_ANY
import struct

UDP_size = 1472
header_size = 8
max_payload_size = UDP_size - header_size

# Given a sorted array, are all sequence numbers
# present starting from the given starting sequence number
def can_return(seq_num, buf):
    for i in range(len(buf)):
        if buf[i][0] != seq_num:
            return False
        seq_num += 1
    return True

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
        self.rec_buf = []

    def send(self, data_bytes: bytes) -> None:
        total_len = len(data_bytes)

        while total_len > 0:
            payload_size = total_len % max_payload_size
            if payload_size == 0:
                payload_size = max_payload_size

            header = struct.pack('2i', self.send_num, payload_size)
            packet = header + data_bytes[:payload_size]
            self.socket.sendto(packet, (self.dst_ip, self.dst_port))
            data_bytes = data_bytes[payload_size:]
            total_len -= payload_size
            self.send_num += 1

    def recv(self) -> bytes:
        """Blocks (waits) if no data is ready to be read from the connection."""
        data, addr = self.socket.recvfrom()
        to_return = b''
        header = struct.unpack('2i', data[:header_size])
        data = data[header_size:]
        seq_num = int(header[0])
        payload_size = int(header[1])
        payload = data[:payload_size]
        self.rec_buf.append([seq_num, payload])
        self.rec_buf = sorted(self.rec_buf, key=lambda p: p[0])
        if can_return(self.rec_num, self.rec_buf):
            for pair in self.rec_buf:
                to_return = to_return + pair[1]
            self.rec_num += len(self.rec_buf)
            self.rec_buf = []
        return to_return

    def close(self) -> None:
        """Cleans up. It should block (wait) until the Streamer is done with all
           the necessary ACKs and retransmissions"""
        # your code goes here, especially after you add ACKs and retransmissions.
        pass
